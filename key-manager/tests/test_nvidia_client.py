"""NvidiaClient 离线单元测试：用 httpx.MockTransport 复现上游各种真实故障形态。

运行：
    python -m pytest key-manager/tests -q

这里覆盖的每一条，都是在真实 NVIDIA 服务上踩过的坑（或为了防住它）：
- HTTP 200 但 body 是错误对象（``Service temporarily overloaded``）→ 必须当失败并换 Key；
- 429 尊重 ``Retry-After``、401/403 长冷却、400/422 直接抛（换 Key 没用）；
- 模型级故障转移只在 5xx / 错误体触发，429 不换模型（额度问题换模型解决不了）；
- 流式：``delta.reasoning_content``、上游不发 ``[DONE]``、流内错误对象、
  空流、以及 stream=True 却回普通 JSON 的上游；
- 滑动窗口打满 → ``KeyUnavailableError``（而不是静默超额）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from key_manager import KeyUnavailableError
from nvidia_client import NvidiaClient, UpstreamError

K1 = "nvapi-offline-0001"
K2 = "nvapi-offline-0002"
MSGS = [{"role": "user", "content": "hi"}]


# ---------- 工具 ----------

def fp(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def auth_key(request: httpx.Request) -> str:
    return request.headers["authorization"].removeprefix("Bearer ")


def body_of(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def ok_body(text: str = "ok") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def sse(*events: str) -> httpx.Response:
    payload = "".join(f"data: {event}\n\n" for event in events)
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=payload.encode("utf-8")
    )


def client_for(keys, handler, **kwargs) -> NvidiaClient:
    """构造一个走假 transport、且不真的 sleep 的客户端。"""
    kwargs.setdefault("retry_delay_seconds", 0)
    kwargs.setdefault("max_attempts", len(keys))
    return NvidiaClient(keys=list(keys), transport=httpx.MockTransport(handler), **kwargs)


# ---------- 非流式：正常与换 Key ----------

def test_chat_returns_text_and_records_latency():
    client = client_for([K1], lambda request: httpx.Response(200, json=ok_body("你好")))
    assert client.chat(MSGS) == "你好"
    assert client.last_key_fingerprint == fp(K1)
    assert client.manager.stats()[K1]["latency_ema_ms"] is not None


def test_reasoning_only_response_is_not_treated_as_empty():
    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"message": {"reasoning_content": "只有思考过程"}}]},
        )

    client = client_for([K1], handler)
    assert client.chat(MSGS) == "只有思考过程"


def test_http_200_with_error_body_switches_key():
    """上游真实怪癖：HTTP 200，body 却是 {"error": ...}。"""

    def handler(request):
        if auth_key(request) == K1:
            return httpx.Response(200, json={"error": {"message": "Service temporarily overloaded"}})
        return httpx.Response(200, json=ok_body())

    client = client_for([K1, K2], handler)
    assert client.chat(MSGS) == "ok"
    assert client.last_key_fingerprint == fp(K2)
    # 这把 Key 要记成 503（而不是 200），否则它会一直被当成健康 Key
    assert client.manager.stats()[K1]["last_status"] == 503


def test_all_keys_return_error_body_raises():
    calls: list[str] = []

    def handler(request):
        calls.append(auth_key(request))
        return httpx.Response(200, json={"error": "Internal server error"})

    client = client_for([K1, K2], handler)
    with pytest.raises(UpstreamError) as excinfo:
        client.chat(MSGS)
    assert excinfo.value.status == 503
    assert set(calls) == {K1, K2}


def test_429_switches_key_and_honours_retry_after():
    def handler(request):
        if auth_key(request) == K1:
            return httpx.Response(
                429, headers={"retry-after": "12"}, json={"error": {"message": "rate limited"}}
            )
        return httpx.Response(200, json=ok_body())

    client = client_for([K1, K2], handler)
    assert client.chat(MSGS) == "ok"
    cooldown = client.manager.stats()[K1]["cooldown_seconds"]
    assert 11 <= cooldown <= 13, f"应该用 Retry-After(12s)，实际 {cooldown}"


def test_401_gets_long_cooldown_but_is_not_permanent():
    def handler(request):
        if auth_key(request) == K1:
            return httpx.Response(401, json={"error": {"message": "unauthorized"}})
        return httpx.Response(200, json=ok_body())

    client = client_for([K1, K2], handler, auth_cooldown_seconds=300)
    assert client.chat(MSGS) == "ok"
    assert client.manager.stats()[K1]["cooldown_seconds"] > 60


def test_400_is_raised_without_trying_other_keys():
    calls: list[str] = []

    def handler(request):
        calls.append(auth_key(request))
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    client = client_for([K1, K2], handler)
    with pytest.raises(UpstreamError) as excinfo:
        client.chat(MSGS)
    assert excinfo.value.status == 400
    assert len(calls) == 1, "参数类错误换 Key 没用，不该重试"


# ---------- 模型级故障转移 ----------

def test_model_fallback_on_persistent_5xx():
    models: list[str] = []

    def handler(request):
        model = body_of(request)["model"]
        models.append(model)
        if model == "model-a":
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=ok_body("from-b"))

    client = client_for(
        [K1], handler, model="model-a", model_fallbacks={"model-a": ["model-b"]}
    )
    assert client.chat(MSGS) == "from-b"
    assert client.last_used_model == "model-b"
    assert client.last_fallbacks == ["model-a"]
    assert models[0] == "model-a" and models[-1] == "model-b"


def test_429_does_not_trigger_model_fallback():
    models: list[str] = []

    def handler(request):
        models.append(body_of(request)["model"])
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = client_for(
        [K1, K2], handler, model="model-a", model_fallbacks={"model-a": ["model-b"]}
    )
    with pytest.raises(UpstreamError) as excinfo:
        client.chat(MSGS)
    assert excinfo.value.status == 429
    assert set(models) == {"model-a"}, "额度问题换模型解决不了，不该换"


# ---------- 流式 ----------

def test_stream_reads_reasoning_content():
    """推理模型常把正文放在 reasoning_content 里，只读 content 会误报空响应。"""

    def handler(request):
        return sse(
            json.dumps({"choices": [{"delta": {"reasoning_content": "思"}}]}),
            json.dumps({"choices": [{"delta": {"reasoning_content": "考"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )

    client = client_for([K1], handler)
    assert "".join(client.chat_stream(MSGS)) == "思考"


def test_stream_without_done_terminator_still_finishes():
    def handler(request):
        # 故意不发 data: [DONE]
        return sse(
            json.dumps({"choices": [{"delta": {"content": "abc"}}]}),
            json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        )

    client = client_for([K1], handler)
    assert "".join(client.chat_stream(MSGS)) == "abc"


def test_stream_in_band_error_after_content_is_partial_and_not_retried():
    calls: list[str] = []

    def handler(request):
        calls.append(auth_key(request))
        return sse(
            json.dumps({"choices": [{"delta": {"content": "半句"}}]}),
            json.dumps({"error": {"message": "Internal server error"}}),
        )

    client = client_for([K1, K2], handler)
    chunks: list[str] = []
    with pytest.raises(UpstreamError) as excinfo:
        for piece in client.chat_stream(MSGS):
            chunks.append(piece)
    assert chunks == ["半句"]
    assert excinfo.value.partial is True
    assert len(calls) == 1, "已经吐过内容，换 Key 重发会导致重复输出"


def test_stream_empty_stream_retries_next_key():
    def handler(request):
        if auth_key(request) == K1:
            return sse(json.dumps({"choices": [{"delta": {}}]}))  # 一个字的正文都没有
        return sse(json.dumps({"choices": [{"delta": {"content": "ok"}}]}))

    client = client_for([K1, K2], handler)
    assert "".join(client.chat_stream(MSGS)) == "ok"


def test_stream_true_but_json_body_is_handled():
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, json=ok_body("整段返回")
        )

    client = client_for([K1], handler)
    assert "".join(client.chat_stream(MSGS)) == "整段返回"


def test_chat_with_stream_true_returns_accumulated_text():
    def handler(request):
        assert body_of(request)["stream"] is True
        return sse(
            json.dumps({"choices": [{"delta": {"content": "流"}}]}),
            json.dumps({"choices": [{"delta": {"content": "式"}}]}),
        )

    client = client_for([K1], handler)
    assert client.chat(MSGS, stream=True) == "流式"


# ---------- 限流与连接复用 ----------

def test_quota_exhausted_raises_key_unavailable():
    client = client_for(
        [K1], lambda request: httpx.Response(200, json=ok_body()), rpm_per_key=1, max_attempts=1
    )
    assert client.chat(MSGS) == "ok"
    with pytest.raises(KeyUnavailableError):
        client.chat(MSGS)


def test_http_client_is_reused_between_requests():
    client = client_for([K1], lambda request: httpx.Response(200, json=ok_body()))
    try:
        assert client._get_client() is client._get_client()
    finally:
        client.close()


def test_context_manager_closes_client():
    with client_for([K1], lambda request: httpx.Response(200, json=ok_body())) as client:
        assert client.chat(MSGS) == "ok"
    assert client._http_client is None


def test_model_fallback_relaxes_cooldown_but_never_quota():
    """换模型可以放过冷却，但不能突破窗口限流：额度是真的，换模型也变不出来。"""

    def handler(request):
        model = body_of(request)["model"]
        if model == "model-a":
            return httpx.Response(503, json={"error": {"message": "overloaded"}})
        return httpx.Response(200, json=ok_body("from-b"))

    client = client_for(
        [K1],
        handler,
        model="model-a",
        model_fallbacks={"model-a": ["model-b"]},
        rpm_per_key=1,
        max_attempts=1,
    )
    with pytest.raises(KeyUnavailableError):
        client.chat(MSGS)


@pytest.mark.parametrize("status", [403, 404, 410])
def test_model_gone_or_forbidden_triggers_model_fallback(status):
    """模型已下线/改名（404/410）或当前账号无权限（403）：换 Key 没用，换模型才有用。"""

    def handler(request):
        if body_of(request)["model"] == "model-a":
            return httpx.Response(status, json={"error": {"message": "model not available"}})
        return httpx.Response(200, json=ok_body("from-b"))

    client = client_for([K1, K2], handler, model="model-a", model_fallbacks={"model-a": ["model-b"]})
    assert client.chat(MSGS) == "from-b"
    assert client.last_used_model == "model-b"


def test_401_does_not_trigger_model_fallback():
    """Key 失效（401）：该换 Key（已经换过两把了），不该继续折腾模型。"""
    models: list[str] = []

    def handler(request):
        models.append(body_of(request)["model"])
        return httpx.Response(401, json={"error": {"message": "unauthorized"}})

    client = client_for([K1, K2], handler, model="model-a", model_fallbacks={"model-a": ["model-b"]})
    with pytest.raises(UpstreamError) as excinfo:
        client.chat(MSGS)
    assert excinfo.value.status == 401
    assert set(models) == {"model-a"}


def test_model_level_error_does_not_burn_every_key():
    """模型已下线时，只试 2 把 Key 就换模型：免费层额度不该烧在死模型上。"""
    attempts: list[tuple[str, str]] = []

    def handler(request):
        model = body_of(request)["model"]
        attempts.append((auth_key(request), model))
        if model == "model-a":
            return httpx.Response(410, json={"error": {"message": "model retired"}})
        return httpx.Response(200, json=ok_body("from-b"))

    client = client_for(
        [K1, K2, "nvapi-offline-0003", "nvapi-offline-0004", "nvapi-offline-0005"],
        handler,
        model="model-a",
        model_fallbacks={"model-a": ["model-b"]},
    )
    assert client.chat(MSGS) == "from-b"
    dead_attempts = [item for item in attempts if item[1] == "model-a"]
    assert len(dead_attempts) == 2, f"死模型只该试 2 把 Key，实际 {len(dead_attempts)} 把"
    assert attempts[-1][1] == "model-b"


def test_stream_model_level_error_also_switches_model_early():
    attempts: list[tuple[str, str]] = []

    def handler(request):
        model = body_of(request)["model"]
        attempts.append((auth_key(request), model))
        if model == "model-a":
            return httpx.Response(404, json={"error": {"message": "model gone"}})
        return sse(json.dumps({"choices": [{"delta": {"content": "ok"}}]}))

    client = client_for(
        [K1, K2, "nvapi-offline-0003", "nvapi-offline-0004"],
        handler,
        model="model-a",
        model_fallbacks={"model-a": ["model-b"]},
    )
    assert "".join(client.chat_stream(MSGS)) == "ok"
    assert len([item for item in attempts if item[1] == "model-a"]) == 2
