"""KeyManager 单元测试（纯逻辑，零外部依赖）。

运行：
    python -m pytest key-manager/tests -q

覆盖：旧策略行为、滑动窗口限流、least_loaded、按状态分级冷却、Retry-After、
延迟 EMA、向后兼容的旧调用方式、线程安全。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from key_manager import KeyManager, KeyUnavailableError


def _unlimited(keys, strategy: str = "round_robin", **kwargs) -> KeyManager:
    """构造不做窗口限流的 KeyManager，用于验证原有策略行为。"""
    return KeyManager(keys, strategy=strategy, rpm_per_key=None, **kwargs)


# ---------- 基础策略（保持旧行为） ----------

def test_round_robin_cycles():
    km = _unlimited(["a", "b", "c"])
    got = [km.next() for _ in range(6)]
    assert got == ["a", "b", "c", "a", "b", "c"]


def test_random_returns_known_key():
    km = _unlimited(["a", "b"], strategy="random")
    for _ in range(50):
        assert km.next() in {"a", "b"}


def test_weighted_only_chooses_weighted_keys():
    # 权重为 0 的 key 永远不应被抽中
    km = _unlimited([("a", 0), ("b", 10)], strategy="weighted")
    for _ in range(200):
        assert km.next() == "b"


def test_weighted_zero_total_does_not_crash():
    km = _unlimited([("a", 0), ("b", 0)], strategy="weighted")
    assert km.next() in {"a", "b"}


def test_weighted_distribution_roughly_matches():
    km = _unlimited([("a", 5), ("b", 1)], strategy="weighted")
    counts = {"a": 0, "b": 0}
    n = 6000
    for _ in range(n):
        counts[km.next()] += 1
    # a:b 期望 5:1，容差放宽避免不稳定
    assert 0.7 < counts["a"] / counts["b"] < 6.0


# ---------- 入参校验 ----------

def test_weighted_with_plain_strings_raises_readable_error():
    with pytest.raises(ValueError) as exc:
        KeyManager(keys=["a", "b"], strategy="weighted")
    assert "(key, weight)" in str(exc.value)


def test_weighted_negative_weight_rejected():
    with pytest.raises(ValueError) as exc:
        KeyManager(keys=[("a", -1)], strategy="weighted")
    assert "非负" in str(exc.value)


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError) as exc:
        KeyManager(keys=["a"], strategy="nope")
    assert "round_robin" in str(exc.value)


def test_keys_property_returns_copy():
    km = _unlimited(["a", "b"])
    keys = km.keys
    keys.append("c")
    assert km.keys == ["a", "b"]


# ---------- 失败冷却 / 恢复 ----------

def test_failure_puts_key_on_cooldown():
    km = KeyManager(keys=["a", "b"], rpm_per_key=40)
    km.report_failure("a", cooldown_seconds=60)
    # 冷却期内 a 不应被返回
    for _ in range(20):
        assert km.next() == "b"
    stats = km.stats()
    assert stats["a"]["failures"] == 1
    assert stats["a"]["total_failures"] == 1
    assert stats["a"]["cooldown_seconds"] > 0


def test_success_clears_cooldown_and_consecutive_failures():
    km = KeyManager(keys=["a", "b"], rpm_per_key=40)
    km.report_failure("a", cooldown_seconds=60)
    km.report_success("a")
    got = {km.next() for _ in range(10)}
    assert "a" in got
    stats = km.stats()
    assert stats["a"]["failures"] == 0
    assert stats["a"]["total_failures"] == 1
    assert stats["a"]["latency_ema_ms"] is None


def test_all_keys_cooling_down_reports_cooling_with_wait():
    # 旧行为是“仍然返回一个 Key”；新语义改为抛错，并说明还要等多久
    km = KeyManager(keys=["a", "b"], rpm_per_key=40)
    km.report_failure("a", cooldown_seconds=60)
    km.report_failure("b", cooldown_seconds=60)
    with pytest.raises(KeyUnavailableError) as exc:
        km.next()
    assert exc.value.reason == "cooling"
    assert exc.value.retry_after is not None
    assert 0 < exc.value.retry_after <= 60


def test_empty_pool_reports_empty():
    km = KeyManager(keys=[], rpm_per_key=40)
    with pytest.raises(KeyUnavailableError) as exc:
        km.next()
    assert exc.value.reason == "empty"


# ---------- 滑动窗口限流 ----------

def test_rpm_window_limits_and_recovers_after_roll():
    km = KeyManager(keys=["a"], rpm_per_key=2, window_seconds=0.3)
    assert km.next() == "a"
    assert km.next() == "a"
    with pytest.raises(KeyUnavailableError) as exc:
        km.next()
    assert exc.value.reason == "rate_limited"
    assert exc.value.retry_after is not None
    assert 0 < exc.value.retry_after <= 0.3

    time.sleep(0.35)
    assert km.next() == "a"  # 窗口滚动后额度恢复


def test_rpm_window_spreads_across_keys():
    km = KeyManager(keys=["a", "b"], strategy="least_loaded", rpm_per_key=1, window_seconds=60)
    picked = {km.next(), km.next()}
    assert picked == {"a", "b"}
    with pytest.raises(KeyUnavailableError) as exc:
        km.next()
    assert exc.value.reason == "rate_limited"


def test_rpm_per_account_limits_shared_account():
    km = KeyManager(
        keys=["a", "b"],
        rpm_per_key=10,
        rpm_per_account=1,
        accounts={"a": "acct-1", "b": "acct-1"},
    )
    assert km.next() == "a"
    with pytest.raises(KeyUnavailableError) as exc:
        km.next()
    assert exc.value.reason == "rate_limited"
    # 账号维度和 Key 维度取交集：换到 b 也不算可用
    assert km.stats()["b"]["remaining"] == 0


def test_headroom_reports_remaining_quota():
    km = KeyManager(keys=["a", "b"], strategy="round_robin", rpm_per_key=3)
    assert km.next() == "a"
    assert km.headroom("a") == 2
    assert km.headroom("b") == 3
    stats = km.stats()
    assert stats["a"]["used_in_window"] == 1
    assert stats["b"]["latency_ema_ms"] is None


def test_unlimited_headroom_is_none():
    km = _unlimited(["a"])
    assert km.headroom("a") is None
    assert km.stats()["a"]["remaining"] is None


# ---------- least_loaded / smart ----------

def test_least_loaded_picks_key_with_most_headroom():
    km = KeyManager(keys=["a", "b"], strategy="least_loaded", rpm_per_key=2)
    # 初始并列 → a；之后 b 余量更大 → b；如此交替
    assert km.next() == "a"
    assert km.next() == "b"
    assert km.next() == "a"
    assert km.next() == "b"


def test_least_loaded_tie_breaks_on_latency_ema():
    km = KeyManager(keys=["a", "b"], strategy="least_loaded", rpm_per_key=10)
    km.report_success("a", latency_ms=500)
    km.report_success("b", latency_ms=50)
    assert km.next() == "b"  # 余量相同，延迟 EMA 更小者优先


def test_least_loaded_headroom_beats_latency():
    km = KeyManager(keys=["a", "b"], strategy="least_loaded", rpm_per_key=2)
    km.report_success("a", latency_ms=1)
    km.report_success("b", latency_ms=100)
    assert km.next() == "a"  # 余量相同，a 更快
    assert km.next() == "b"  # a 余量只剩 1，b 还有 2


def test_smart_alias_for_least_loaded():
    km = KeyManager(keys=["a", "b"], strategy="smart", rpm_per_key=10)
    assert km.next() in {"a", "b"}


# ---------- 按状态分级冷却 ----------

def test_auth_error_gets_long_cooldown_but_not_permanent():
    km = KeyManager(keys=["a"], rpm_per_key=40, auth_cooldown_seconds=300)
    km.report_failure("a", status=401)
    remaining = km.stats()["a"]["cooldown_seconds"]
    assert 250 <= remaining <= 300
    # 不是永久拉黑：只是一个有限冷却
    assert remaining < 1000


def test_auth_cooldown_is_configurable():
    km = KeyManager(keys=["a"], rpm_per_key=40, auth_cooldown_seconds=12)
    km.report_failure("a", status=403)
    assert km.stats()["a"]["cooldown_seconds"] <= 12


def test_429_without_retry_after_uses_default():
    km = KeyManager(keys=["a"], rpm_per_key=40, cooldown_429_seconds=20)
    km.report_failure("a", status=429)
    remaining = km.stats()["a"]["cooldown_seconds"]
    assert 18 <= remaining <= 20.5


def test_429_respects_retry_after():
    km = KeyManager(keys=["a"], rpm_per_key=40)
    km.report_failure("a", status=429, retry_after=7)
    remaining = km.stats()["a"]["cooldown_seconds"]
    assert 6 <= remaining <= 7.5


def test_5xx_gets_short_cooldown():
    km = KeyManager(keys=["a"], rpm_per_key=40, cooldown_5xx_seconds=2)
    km.report_failure("a", status=503)
    remaining = km.stats()["a"]["cooldown_seconds"]
    assert 0 <= remaining <= 2.1


def test_client_error_does_not_cooldown_key():
    km = KeyManager(keys=["a"], rpm_per_key=40)
    km.report_failure("a", status=400)
    assert km.stats()["a"]["cooldown_seconds"] == 0
    assert km.next() == "a"


def test_legacy_cooldown_seconds_still_works():
    km = KeyManager(keys=["a"], rpm_per_key=40)
    km.report_failure("a", cooldown_seconds=60)
    remaining = km.stats()["a"]["cooldown_seconds"]
    assert 58 <= remaining <= 60.5


# ---------- 延迟 EMA ----------

def test_latency_ema_is_smoothed():
    km = KeyManager(keys=["a"], rpm_per_key=40, latency_ema_alpha=0.3)
    km.report_success("a", latency_ms=100)
    assert km.stats()["a"]["latency_ema_ms"] == pytest.approx(100)
    km.report_success("a", latency_ms=200)
    assert km.stats()["a"]["latency_ema_ms"] == pytest.approx(130)  # 0.7*100 + 0.3*200


# ---------- exclude / relax ----------

def test_exclude_skips_tried_keys():
    km = KeyManager(keys=["a", "b"], rpm_per_key=10)
    assert km.next(exclude={"a"}) == "b"
    with pytest.raises(KeyUnavailableError) as exc:
        km.next(exclude={"a", "b"})
    assert exc.value.reason == "excluded"


def test_relax_ignores_cooldown_but_not_quota():
    km = KeyManager(keys=["a"], rpm_per_key=1)
    km.report_failure("a", status=503, cooldown_seconds=60)
    with pytest.raises(KeyUnavailableError):
        km.next()
    assert km.next(relax=True) == "a"  # 模型级故障转移：忽略冷却
    with pytest.raises(KeyUnavailableError) as exc:
        km.next(relax=True)  # 但窗口额度不放宽
    assert exc.value.reason == "rate_limited"


# ---------- 线程安全 ----------

def test_concurrent_next_respects_window():
    km = KeyManager(keys=["a"], rpm_per_key=50, window_seconds=60)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        for _ in range(10):
            try:
                key = km.next()
            except KeyUnavailableError:
                continue
            with results_lock:
                results.append(key)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 50  # 100 次并发请求恰好只有 50 次拿到额度
    assert km.headroom("a") == 0
