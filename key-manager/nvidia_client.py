"""NVIDIA NIM（OpenAI 兼容 HTTP 接口）多 Key 客户端。

特性：
- 多 Key 调度 + 滑动窗口限流（默认 40 次/分钟/Key）；
- 失败分级冷却：401/403 长冷却、429 尊重 Retry-After、5xx 短冷却；
- “HTTP 200 但响应体是错误对象”也按失败处理并换 Key；
- 模型级故障转移：某个模型试遍 Key 仍是上游 5xx/错误体时，按 ``model_fallbacks`` 换模型；
  模型已下线/改名或无权限（404/410/403）时最多只试 2 把 Key 就换模型，不把免费层额度烧在死模型上；
- 流式 SSE：同时解析 ``delta.content`` 与 ``delta.reasoning_content``，没有 ``[DONE]``
  也能用 ``finish_reason`` 正常收尾，流内错误对象会当失败。

模块本身**不依赖 openai / python-dotenv**：默认用 httpx 连接池发请求（按 Key 复用连接），
因此没装 openai 也能 import、也能用假 transport 做离线测试。只有在需要从环境变量读取
Key 时，才会尝试懒加载 python-dotenv（未安装则跳过）。

用法：
    from nvidia_client import NvidiaClient
    client = NvidiaClient()
    reply = client.chat([{"role": "user", "content": "你好"}])
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import httpx

from key_manager import KeyManager, KeyUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_MODEL = "nvidia/llama-3.1-nemotron-70b-instruct"

# 上游整片故障时的模型级备用链；可在构造时用 model_fallbacks 覆盖/追加
DEFAULT_MODEL_FALLBACKS: dict[str, list[str]] = {
    "deepseek-ai/deepseek-v4-pro-0813": ["nvidia/nemotron-3-super-120b-a12b"],
}

# 模型级错误：模型已下线/改名（404/410）或当前账号没有该模型权限（403）。
# 这类错误换 Key 基本没用（额度是真的、模型是真的没了），所以最多试这么多把 Key
# 就早点交给模型级故障转移 —— 免费层每把 Key 只有 40 次/分钟，不该烧在死模型上。
MODEL_LEVEL_STATUSES = frozenset({403, 404, 410})
MODEL_LEVEL_KEY_ATTEMPTS = 2

_SSE_DONE = object()
_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """懒加载 python-dotenv：没装这个包也不影响本模块的导入与测试。"""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv  # noqa: PLC0415 —— 故意延迟到真正需要时
    except ImportError:
        return
    load_dotenv()


def _split_keys(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def _fingerprint(key: str) -> str:
    """只用于日志的 sha256 前 8 位指纹，绝不打印完整 Key。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def _retry_after_seconds(value: str | None) -> float | None:
    """解析 Retry-After 头（秒）；HTTP-date 等无法解析的形式返回 None。"""
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _error_message_from_payload(payload: Any) -> str | None:
    """识别 HTTP 200 响应体里的错误对象，返回可读错误信息。"""
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error:
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or error
        else:
            message = error
        return str(message)
    if str(payload.get("type", "")).lower() == "error":
        return str(payload.get("message") or payload)
    return None


def _raise_if_error_payload(payload: Any) -> None:
    """NVIDIA 真实存在的故障形态：HTTP 200 但 body 是 {"error": ...}。"""
    message = _error_message_from_payload(payload)
    if message is not None:
        raise UpstreamError(503, f"上游以 200 返回错误体：{message}")


def _extract_message_text(payload: Any) -> str:
    """从非流式响应里取文本，兼容 content / reasoning_content / text。"""
    choices = payload.get("choices") if isinstance(payload, dict) else None
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    for choice in choices or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = message.get("content")
        if content:
            content_parts.append(str(content))
        reasoning = message.get("reasoning_content")
        if reasoning:
            reasoning_parts.append(str(reasoning))
        text = choice.get("text")
        if text:
            content_parts.append(str(text))
    text = "".join(content_parts).strip() or "".join(reasoning_parts).strip()
    if not text:
        raise UpstreamError(502, "上游返回空响应")
    return text


def _parse_sse_line(line: str) -> str | object | None:
    """解析一行 SSE：返回 data 字符串、_SSE_DONE 标记或 None（忽略）。"""
    if not line:
        return None
    if line.startswith(":"):
        return None
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data:
        return None
    if data == "[DONE]":
        return _SSE_DONE
    return data


def _loads_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


class UpstreamError(RuntimeError):
    """上游返回了失败响应（含 HTTP 200 裹错误体、流内错误对象）。"""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        retry_after: float | None = None,
        partial: bool = False,
    ) -> None:
        self.status = int(status)
        self.text = str(message)
        self.retry_after = retry_after
        # partial=True 表示流式响应已经吐了一部分给调用方，不能再换 Key/模型重发
        self.partial = partial
        super().__init__(f"上游 {self.status}：{self.text[:300]}")


class NvidiaClient:
    """带多 Key 调度、限流、失败分级冷却、模型级故障转移的 NVIDIA 客户端。"""

    def __init__(
        self,
        keys: list[str] | None = None,
        *,
        base_url: str | None = None,
        model: str | None = None,
        strategy: str = "round_robin",
        max_attempts: int | None = None,
        cooldown_seconds: float = 60.0,
        rpm_per_key: int | None = 40,
        rpm_per_account: int | None = None,
        accounts: dict[str, str] | None = None,
        auth_cooldown_seconds: float = 300.0,
        cooldown_429_seconds: float = 20.0,
        cooldown_5xx_seconds: float = 2.0,
        model_fallbacks: dict[str, list[str]] | None = None,
        model_retry_budget_s: float = 7.0,
        fallback_max: int = 1,
        timeout: float = 60.0,
        retry_delay_seconds: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not keys:
            _load_dotenv_once()
            keys = _split_keys(os.getenv("NVIDIA_API_KEYS"))
        if not keys:
            raise ValueError(
                "没有可用的 API Key：请设置环境变量 NVIDIA_API_KEYS "
                "（逗号分隔）或直接传入 keys 参数。"
            )

        self.base_url = (base_url or os.getenv("NVIDIA_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.getenv("NVIDIA_MODEL", DEFAULT_MODEL)
        self.manager = KeyManager(
            keys,
            strategy=strategy,
            rpm_per_key=rpm_per_key,
            rpm_per_account=rpm_per_account,
            accounts=accounts,
            default_cooldown_seconds=cooldown_seconds,
            auth_cooldown_seconds=auth_cooldown_seconds,
            cooldown_429_seconds=cooldown_429_seconds,
            cooldown_5xx_seconds=cooldown_5xx_seconds,
        )
        self.max_attempts = max_attempts or len(self.manager.keys)
        self.model_retry_budget_s = float(model_retry_budget_s)
        self.fallback_max = max(0, int(fallback_max))
        self.retry_delay_seconds = float(retry_delay_seconds)
        self.timeout = float(timeout)

        merged_fallbacks = dict(DEFAULT_MODEL_FALLBACKS)
        for fallback_model, targets in (model_fallbacks or {}).items():
            merged_fallbacks[str(fallback_model)] = [str(target) for target in targets]
        self.model_fallbacks = merged_fallbacks

        self._transport = transport
        self._http_client = http_client
        self._owns_client = False

        # 供调用方/日志观察“这一次实际用了哪个模型、发生过哪些故障转移”
        self.last_used_model: str | None = None
        self.last_fallbacks: list[str] = []
        self.last_key_fingerprint: str | None = None
        self.last_model_budget_exceeded = False

    # ---------- 连接池 ----------

    def _get_client(self) -> httpx.Client:
        """共享一个 httpx.Client：连接池 + TLS 会话复用，避免每次重试重建连接。"""
        if self._http_client is None:
            self._http_client = httpx.Client(timeout=self.timeout, transport=self._transport)
            self._owns_client = True
        return self._http_client

    def close(self) -> None:
        if self._http_client is not None and self._owns_client:
            self._http_client.close()
        self._http_client = None
        self._owns_client = False

    def __enter__(self) -> "NvidiaClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    @staticmethod
    def _headers(key: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    # ---------- 对外入口 ----------

    def chat(self, messages: list[dict], **kwargs) -> str:
        """发送对话请求并返回模型回复文本。

        失败自动换 Key；某个模型试遍 Key 仍是上游 5xx/错误体时按 ``model_fallbacks``
        换下一个模型。``stream=True`` 时逐块解析 SSE 并返回累积文本；需要逐块回调时用
        :meth:`chat_stream`。被换过的模型可从 ``last_used_model`` / ``last_fallbacks``
        或日志中看到。
        """
        stream = bool(kwargs.pop("stream", False))
        if stream:
            return "".join(self._iter_chat(messages, kwargs))
        return self._chat_json(messages, kwargs)

    def chat_stream(self, messages: list[dict], **kwargs) -> Iterator[str]:
        """流式请求，逐块 yield 文本增量（``content`` 与 ``reasoning_content``）。

        重试/换 Key/换模型的规则与 :meth:`chat` 相同；但一旦已经有内容吐给调用方，
        就不会再换 Key 重发（否则会重复输出），而是直接抛出错误。
        """
        kwargs.pop("stream", None)
        return self._iter_chat(messages, kwargs)

    def _chat_json(self, messages: list[dict], kwargs: dict) -> str:
        requested_model = str(kwargs.pop("model", None) or self.model)
        chain = self._model_chain(requested_model)
        last_error: UpstreamError | None = None
        for index, model in enumerate(chain):
            if index > 0:
                logger.warning("模型级故障转移：%s -> %s", chain[index - 1], model)
            payload = dict(kwargs)
            payload["model"] = model
            payload["messages"] = messages
            payload["stream"] = False
            try:
                text = self._request_json_with_retry(model, payload, relax=index > 0)
            except UpstreamError as exc:
                last_error = exc
                if index < len(chain) - 1 and self._should_switch_model(exc):
                    continue
                raise
            self.last_used_model = model
            self.last_fallbacks = list(chain[:index])
            return text
        raise last_error if last_error is not None else UpstreamError(503, "没有可用的模型")

    def _iter_chat(self, messages: list[dict], kwargs: dict) -> Iterator[str]:
        requested_model = str(kwargs.pop("model", None) or self.model)
        chain = self._model_chain(requested_model)
        last_error: UpstreamError | None = None
        for index, model in enumerate(chain):
            if index > 0:
                logger.warning("模型级故障转移：%s -> %s", chain[index - 1], model)
            payload = dict(kwargs)
            payload["model"] = model
            payload["messages"] = messages
            payload["stream"] = True
            try:
                yield from self._iter_model_stream(model, payload, relax=index > 0)
            except UpstreamError as exc:
                last_error = exc
                if index < len(chain) - 1 and self._should_switch_model(exc):
                    continue
                raise
            self.last_used_model = model
            self.last_fallbacks = list(chain[:index])
            return
        raise last_error if last_error is not None else UpstreamError(503, "没有可用的模型")

    def _model_chain(self, model: str) -> list[str]:
        chain = [model]
        for fallback in self.model_fallbacks.get(model, []):
            if fallback and fallback not in chain:
                chain.append(fallback)
            if len(chain) >= 1 + self.fallback_max:
                break
        return chain

    @staticmethod
    def _should_switch_model(exc: UpstreamError) -> bool:
        """判断“换模型”是否可能解决问题。

        - 该换：上游整片 5xx / HTTP 200 裹错误体；以及 403/404/410
          —— 模型已下线、改名，或当前账号没有该模型的权限：这些换 Key 没用、换模型有用；
        - 不该换：429（额度问题，换模型变不出额度）与 401（Key 失效，该换 Key）。
        """
        if getattr(exc, "partial", False):
            # 已经有内容吐给调用方，换模型会让输出重复
            return False
        return exc.status >= 500 or exc.status in (403, 404, 410)

    def _sleep_between_attempts(self) -> None:
        if self.retry_delay_seconds > 0:
            time.sleep(self.retry_delay_seconds)

    # ---------- 非流式 ----------

    def _request_json_with_retry(self, model: str, payload: dict, relax: bool = False) -> str:
        tried: set[str] = set()
        last_error: UpstreamError | None = None
        model_started = time.monotonic()
        for attempt in range(self.max_attempts):
            if self._budget_exceeded(attempt, model_started):
                break
            key = self._next_key(tried, last_error, relax=relax)
            tried.add(key)
            started = time.perf_counter()
            try:
                text = self._post_json(payload, key)
            except UpstreamError as exc:
                self.manager.report_failure(key, status=exc.status, retry_after=exc.retry_after)
                if exc.status in (400, 422):
                    raise
                last_error = exc
                if exc.status in MODEL_LEVEL_STATUSES and len(tried) >= MODEL_LEVEL_KEY_ATTEMPTS:
                    raise  # 模型没了/没权限：交给模型级故障转移，别继续烧 Key 额度
                self._sleep_between_attempts()
                continue
            except httpx.HTTPError as exc:
                # 网络/超时类错误和 Key 本身关系不大：短暂冷却后换一把继续试
                self.manager.report_failure(key, cooldown_seconds=self.manager.cooldown_5xx_seconds)
                last_error = UpstreamError(502, f"网络异常：{exc}")
                self._sleep_between_attempts()
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            self.manager.report_success(key, latency_ms=latency_ms)
            self.last_key_fingerprint = _fingerprint(key)
            return text
        if last_error is not None:
            raise last_error
        raise UpstreamError(503, f"模型 {model} 在当前预算内没有可用 Key")

    def _post_json(self, payload: dict, key: str) -> str:
        client = self._get_client()
        try:
            response = client.post(self._endpoint(), headers=self._headers(key), json=payload)
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, f"请求超时：{exc}") from exc
        if response.status_code >= 400:
            raise UpstreamError(
                response.status_code,
                response.text[:2000],
                retry_after=_retry_after_seconds(response.headers.get("retry-after")),
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise UpstreamError(502, f"响应不是合法 JSON：{response.text[:200]}") from exc
        _raise_if_error_payload(data)
        return _extract_message_text(data)

    # ---------- 流式 ----------

    def _iter_model_stream(
        self, model: str, payload: dict, relax: bool = False
    ) -> Iterator[str]:
        tried: set[str] = set()
        last_error: UpstreamError | None = None
        model_started = time.monotonic()
        for attempt in range(self.max_attempts):
            if self._budget_exceeded(attempt, model_started):
                break
            key = self._next_key(tried, last_error, relax=relax)
            tried.add(key)
            started = time.perf_counter()
            produced = False
            try:
                for chunk in self._iter_sse(payload, key):
                    produced = True
                    yield chunk
            except UpstreamError as exc:
                self.manager.report_failure(key, status=exc.status, retry_after=exc.retry_after)
                if exc.status in (400, 422):
                    raise
                if produced:
                    exc.partial = True
                    raise
                last_error = exc
                if exc.status in MODEL_LEVEL_STATUSES and len(tried) >= MODEL_LEVEL_KEY_ATTEMPTS:
                    raise  # 同上：模型级错误不值得把每把 Key 都试一遍
                self._sleep_between_attempts()
                continue
            except httpx.HTTPError as exc:
                self.manager.report_failure(key, cooldown_seconds=self.manager.cooldown_5xx_seconds)
                if produced:
                    raise UpstreamError(502, f"流式传输中断：{exc}", partial=True) from exc
                last_error = UpstreamError(502, f"网络异常：{exc}")
                self._sleep_between_attempts()
                continue
            latency_ms = (time.perf_counter() - started) * 1000
            self.manager.report_success(key, latency_ms=latency_ms)
            self.last_key_fingerprint = _fingerprint(key)
            return
        if last_error is not None:
            raise last_error
        raise UpstreamError(503, f"模型 {model} 在当前预算内没有可用 Key")

    def _iter_sse(self, payload: dict, key: str) -> Iterator[str]:
        client = self._get_client()
        try:
            with client.stream(
                "POST", self._endpoint(), headers=self._headers(key), json=payload
            ) as response:
                if response.status_code >= 400:
                    body = response.read().decode("utf-8", errors="ignore")
                    raise UpstreamError(
                        response.status_code,
                        body[:2000],
                        retry_after=_retry_after_seconds(response.headers.get("retry-after")),
                    )
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    body = response.read().decode("utf-8", errors="ignore")
                    yield self._text_from_non_sse_body(body)
                    return

                content_parts: list[str] = []
                reasoning_parts: list[str] = []
                for line in response.iter_lines():
                    event = _parse_sse_line(line)
                    if event is _SSE_DONE:
                        break
                    if event is None:
                        continue
                    obj = _loads_or_none(event)
                    if obj is None:
                        continue
                    _raise_if_error_payload(obj)
                    finished = False
                    for choice in obj.get("choices") or []:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            content_parts.append(str(content))
                            yield str(content)
                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            reasoning_parts.append(str(reasoning))
                            yield str(reasoning)
                        if choice.get("finish_reason"):
                            finished = True
                    if finished:
                        # 有的上游不发 data: [DONE]，以 finish_reason 正常收尾，别挂死
                        break
                if not content_parts and not reasoning_parts:
                    raise UpstreamError(502, "上游返回空流")
        except httpx.TimeoutException as exc:
            raise UpstreamError(504, f"流式请求超时：{exc}") from exc

    @staticmethod
    def _text_from_non_sse_body(body: str) -> str:
        """少数上游在 stream=True 时回普通 JSON/纯文本；也要识别错误对象。"""
        try:
            data = json.loads(body)
        except ValueError:
            text = body.strip()
            if not text:
                raise UpstreamError(502, "上游返回空流")
            return text
        _raise_if_error_payload(data)
        return _extract_message_text(data)

    # ---------- 内部工具 ----------

    def _budget_exceeded(self, attempt: int, model_started: float) -> bool:
        """单个模型最多花多久换 Key；超时就换模型，别把额度耗在坏模型上。"""
        if attempt <= 0 or self.model_retry_budget_s <= 0:
            return False
        if time.monotonic() - model_started > self.model_retry_budget_s:
            self.last_model_budget_exceeded = True
            return True
        return False

    def _next_key(
        self, tried: set[str], last_error: UpstreamError | None, *, relax: bool = False
    ) -> str:
        """取一把可用 Key。

        ``relax=True``：放过冷却 —— 换备用模型时才用，因为那点冷却往往正是上一个
        模型打出来的 5xx（不放过就会出现“换了模型却一把 Key 都拿不到”的故障墙）。
        注意它**不放宽窗口限流**：额度是真的，换模型也变不出来。
        """
        try:
            return self.manager.next(exclude=tried or None, relax=relax)
        except KeyUnavailableError:
            # 已经试过的上游错误比“没 Key 了”更有诊断价值，交给模型级故障转移判断
            if last_error is not None:
                raise last_error
            raise
