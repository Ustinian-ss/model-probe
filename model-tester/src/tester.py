from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from models import ModelItem
from providers import Provider


TEST_MESSAGES = [
    {"role": "user", "content": "Reply with OK"}
]


def _error_message_from_payload(obj: Any) -> str | None:
    """识别响应体里的错误对象（含 HTTP 200 裹错误体的形态）。"""
    if not isinstance(obj, dict):
        return None
    error = obj.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error
        else:
            message = error
        return str(message)
    if str(obj.get("type", "")).lower() == "error":
        return str(obj.get("message") or obj)
    return None


def _error_message_from_body(body: str) -> str | None:
    """非 SSE 响应体（普通 JSON）里的错误对象。"""
    try:
        return _error_message_from_payload(json.loads(body))
    except json.JSONDecodeError:
        return None


def parse_sse_chunk_lines(lines: Iterable[str]) -> tuple[str, str | None]:
    """纯函数：解析 SSE 文本行，返回 ``(累积文本, 错误信息)``。

    - 同时读取 ``delta.content`` 与 ``delta.reasoning_content``：推理模型
      （nemotron / gpt-oss 系列）的正文经常只在 reasoning_content 里，只读 content
      会把它误报成 ``Empty streaming response``；
    - 遇到 ``data: [DONE]`` 或某个 choice 的 ``finish_reason`` 即正常收尾：有的上游
      不发 ``[DONE]``，不能因此挂死或误报空响应；
    - ``data: {"error": ...}`` / ``{"type": "error"}`` 视为失败并返回错误信息。
    """
    parts: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            break

        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            # 非 JSON 的 data 块（少数上游的纯文本片段）：保留可读片段，别整段丢弃
            parts.append(data[:80])
            continue

        error = _error_message_from_payload(obj)
        if error is not None:
            return "".join(parts).strip(), error

        finished = False
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            for field in ("content", "reasoning_content"):
                chunk = delta.get(field)
                if chunk:
                    parts.append(str(chunk))
            if choice.get("finish_reason"):
                finished = True
        if finished:
            break
    return "".join(parts).strip(), None


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
        # 批量测试复用同一个 httpx.Client（连接池 + TLS 会话复用），
        # 避免每个请求重建连接导致延迟测量虚高。httpx.Client 线程安全。
        self._client: httpx.Client | None = None

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.provider.timeout_seconds)
        return self._client

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
            client = self._get_client()
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
        with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", errors="ignore")
                return self._result(False, RuntimeError(body[:500]), start)

            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                # 少数上游在 stream=True 时回普通 JSON：先识别错误对象，再退回旧行为取正文
                body = response.read().decode("utf-8", errors="ignore")
                error = _error_message_from_body(body)
                if error:
                    return self._result(False, RuntimeError(error), start)
                text, sse_error = parse_sse_chunk_lines(body.splitlines())
                if sse_error:
                    return self._result(False, RuntimeError(sse_error), start)
                preview = (text or body[:300]).strip()
            else:
                text, sse_error = parse_sse_chunk_lines(response.iter_lines())
                if sse_error:
                    return self._result(False, RuntimeError(sse_error), start)
                preview = text.strip()

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
            client = self._get_client()
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
