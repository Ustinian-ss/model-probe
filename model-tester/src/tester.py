from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from models import ModelItem
from providers import Provider


TEST_MESSAGES = [
    {"role": "user", "content": "Reply with OK"}
]


@dataclass
class TestConfig:
    endpoint_mode: str = "auto"
    prefer_stream: bool = True   # 自动检测时第一步用流式还是非流式
    max_tokens: int = 16
    model: str = ""


class ProviderTester:
    def __init__(self, provider: Provider, config: TestConfig):
        self.provider = provider
        self.config = config
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def test_model(self, item: ModelItem) -> ModelItem:
        item.status = "running"

        if self.cancelled:
            item.status = "skipped"
            return item

        # 根据 endpoint_mode + prefer_stream 决定第一步尝试哪种
        mode = self.config.endpoint_mode
        if mode == "stream":
            return self._probe(item, prefer_stream=True, allow_fallback=False)
        if mode == "non_stream":
            return self._probe(item, prefer_stream=False, allow_fallback=False)
        # auto: 首选 + 失败时自动降级
        return self._probe(item, prefer_stream=self.config.prefer_stream, allow_fallback=True)

    def _probe(self, item: ModelItem, prefer_stream: bool, allow_fallback: bool) -> ModelItem:
        """先按 prefer_stream 试一次；失败且 allow_fallback 则降级到另一种重试一次。"""
        first = self._run_request(item, stream=prefer_stream)
        if first["ok"]:
            item.status = "success"
            item.latency_ms = first["latency_ms"]
            item.streaming_supported = prefer_stream
            item.response_preview = first["preview"]
            return item

        if not allow_fallback:
            item.status = "failed"
            item.last_error = first["error"]
            item.streaming_supported = False
            return item

        # 降级：换另一种
        second = self._run_request(item, stream=not prefer_stream)
        item.status = "success" if second["ok"] else "failed"
        item.latency_ms = second["latency_ms"]
        item.streaming_supported = second["ok"] and (not prefer_stream)
        item.last_error = second["error"]
        item.response_preview = second["preview"]
        return item

    def _run_request(self, item: ModelItem, stream: bool) -> dict[str, Any]:
        base_url = self.provider.base_url.rstrip("/")
        url = f"{base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.provider.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": item.id,
            "messages": TEST_MESSAGES,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }

        start = time.perf_counter()
        try:
            with httpx.Client(timeout=self.provider.timeout_seconds) as client:
                if stream:
                    return self._request_stream(client, url, headers, payload, start)
                return self._request_non_stream(client, url, headers, payload, start)
        except Exception as exc:
            return self._result(False, exc, start)

    def _request_stream(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        start: float,
    ) -> dict[str, Any]:
        preview_parts: list[str] = []
        with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="ignore")
                return self._result(False, RuntimeError(body[:500]), start)

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                body = response.read().decode("utf-8", errors="ignore")
                preview_parts.append(body[:300])
            else:
                for line in response.iter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                            choices = obj.get("choices") or []
                            for choice in choices:
                                delta = choice.get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    preview_parts.append(content)
                        except json.JSONDecodeError:
                            preview_parts.append(data[:80])

        preview = "".join(preview_parts).strip()
        if not preview:
            return self._result(False, RuntimeError("Empty streaming response"), start)

        return self._result(True, None, start, preview[:300])

    def _request_non_stream(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        start: float,
    ) -> dict[str, Any]:
        response = client.post(url, headers=headers, json=payload)
        if response.status_code >= 400:
            return self._result(
                False,
                RuntimeError(response.text[:500]),
                start,
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            return self._result(False, exc, start)

        choices = data.get("choices") or []
        preview_parts: list[str] = []
        for choice in choices:
            message = choice.get("message") or {}
            content = message.get("content")
            if content:
                preview_parts.append(content)

        preview = "".join(preview_parts).strip()
        if not preview:
            return self._result(False, RuntimeError("Empty non-stream response"), start)

        return self._result(True, None, start, preview[:300])

    def fetch_models(self) -> list[str]:
        """调用 {base_url}/models 自动发现 provider 的模型清单。

        兼容 OpenAI 标准返回 {"object": "list", "data": [{"id": ...}, ...]}。
        失败/非标准时返回空列表（由调用方决定是否降级到内置清单）。
        """
        base_url = self.provider.base_url.rstrip("/")
        url = f"{base_url}/models"
        headers = {
            "Authorization": f"Bearer {self.provider.api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.provider.timeout_seconds) as client:
                response = client.get(url, headers=headers)
            if response.status_code >= 400:
                return []
            data = response.json()
        except Exception:
            return []

        ids: list[str] = []
        # 支持两种返回：OpenAI 标准 {"data": [...]} 与少数实现的裸数组
        if isinstance(data, dict):
            payload = data.get("data", data)
        elif isinstance(data, list):
            payload = data
        else:
            payload = []
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
        return ids

    def _result(
        self,
        ok: bool,
        error: Exception | None,
        start: float,
        preview: str | None = None,
    ) -> dict[str, Any]:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "ok": ok,
            "error": str(error) if error else None,
            "latency_ms": latency_ms,
            "preview": preview,
        }
