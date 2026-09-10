"""KeyManager 单元测试（纯逻辑，零外部依赖）。

运行：
    python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from key_manager import KeyManager


# ---------- 基础策略 ----------

def test_round_robin_cycles():
    km = KeyManager(keys=["a", "b", "c"])
    got = [km.next() for _ in range(6)]
    assert got == ["a", "b", "c", "a", "b", "c"]


def test_random_returns_known_key():
    km = KeyManager(keys=["a", "b"], strategy="random")
    for _ in range(50):
        assert km.next() in {"a", "b"}


def test_weighted_only_chooses_weighted_keys():
    # 权重为 0 的 key 永远不应被抽中
    km = KeyManager(keys=[("a", 0), ("b", 10)], strategy="weighted")
    for _ in range(200):
        assert km.next() == "b"


def test_weighted_zero_total_does_not_crash():
    km = KeyManager(keys=[("a", 0), ("b", 0)], strategy="weighted")
    assert km.next() in {"a", "b"}


def test_weighted_distribution_roughly_matches():
    km = KeyManager(keys=[("a", 5), ("b", 1)], strategy="weighted")
    counts = {"a": 0, "b": 0}
    n = 6000
    for _ in range(n):
        counts[km.next()] += 1
    # a:b 期望 5:1，容差放宽避免不稳定
    assert 0.7 < counts["a"] / counts["b"] < 6.0


# ---------- 入参校验（修复：weighted 收到字符串列表应给出可读错误） ----------

def test_weighted_with_plain_strings_raises_readable_error():
    try:
        KeyManager(keys=["a", "b"], strategy="weighted")
    except ValueError as exc:
        assert "(key, weight)" in str(exc)
    else:
        raise AssertionError("应当抛出带说明的 ValueError")


def test_weighted_negative_weight_rejected():
    try:
        KeyManager(keys=[("a", -1)], strategy="weighted")
    except ValueError as exc:
        assert "非负" in str(exc)
    else:
        raise AssertionError("负权重应被拒绝")


def test_unknown_strategy_rejected():
    try:
        KeyManager(keys=["a"], strategy="nope")
    except ValueError as exc:
        assert "round_robin" in str(exc)
    else:
        raise AssertionError("未知策略应被拒绝")


# ---------- 失败冷却 / 恢复 ----------

def test_failure_puts_key_on_cooldown():
    km = KeyManager(keys=["a", "b"])
    km.report_failure("a", cooldown_seconds=60)
    # 冷却期内 a 不应被返回
    for _ in range(20):
        assert km.next() == "b"
    assert km.stats() == {"a": 1}


def test_success_clears_cooldown():
    km = KeyManager(keys=["a", "b"])
    km.report_failure("a", cooldown_seconds=60)
    km.report_success("a")
    got = {km.next() for _ in range(10)}
    assert "a" in got
    assert km.stats() == {}


def test_all_keys_cooling_down_still_returns_something():
    # 全部不可用时 next() 应仍返回一个 key（交由上层报错），而不是死循环/抛异常
    km = KeyManager(keys=["a", "b"])
    km.report_failure("a", cooldown_seconds=60)
    km.report_failure("b", cooldown_seconds=60)
    assert km.next() in {"a", "b"}


def test_keys_property_returns_copy():
    km = KeyManager(keys=["a", "b"])
    keys = km.keys
    keys.append("c")
    assert km.keys == ["a", "b"]
