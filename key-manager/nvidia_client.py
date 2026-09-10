"""NVIDIA NIM（OpenAI 兼容）多 Key 客户端，带失败自动切换。

用法：
    from nvidia_client import NvidiaClient
    client = NvidiaClient()
    reply = client.chat([{"role": "user", "content": "你好"}])
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv
from openai import OpenAI

from key_manager import KeyManager

load_dotenv()

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


class NvidiaClient:
    def __init__(
        self,
        keys: list[str] | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        strategy: str = "round_robin",
        max_attempts: int | None = None,
        cooldown_seconds: float = 60.0,
    ) -> None:
        keys = keys or _split_keys(os.getenv("NVIDIA_API_KEYS"))
        if not keys:
            raise ValueError(
                "没有可用的 API Key：请设置环境变量 NVIDIA_API_KEYS "
                "（逗号分隔）或直接传入 keys 参数。"
            )

        self.base_url = base_url or os.getenv("NVIDIA_BASE_URL", DEFAULT_BASE_URL)
        self.model = model or os.getenv("NVIDIA_MODEL", DEFAULT_MODEL)
        self.manager = KeyManager(keys, strategy=strategy)
        self.max_attempts = max_attempts or len(self.manager.keys)
        self.cooldown_seconds = cooldown_seconds

    def _client(self, key: str) -> OpenAI:
        return OpenAI(base_url=self.base_url, api_key=key)

    def chat(self, messages: list[dict], **kwargs) -> str:
        """发送对话请求，返回模型回复文本。失败自动换 Key 重试。"""
        last_error: Exception | None = None
        for _ in range(self.max_attempts):
            key = self.manager.next()
            try:
                resp = self._client(key).chat.completions.create(
                    model=kwargs.pop("model", self.model),
                    messages=messages,
                    **kwargs,
                )
                self.manager.report_success(key)
                return resp.choices[0].message.content or ""
            except Exception as exc:  # noqa: BLE001 —— 统一处理，避免泄漏 Key
                self.manager.report_failure(key, cooldown_seconds=self.cooldown_seconds)
                last_error = exc
                time.sleep(0.5)
        raise RuntimeError(f"所有 API Key 均失败，最后一次错误：{last_error}")


if __name__ == "__main__":
    client = NvidiaClient()
    print(client.chat([{"role": "user", "content": "用一句话介绍你自己。"}]))
