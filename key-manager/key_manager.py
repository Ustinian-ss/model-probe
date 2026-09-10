"""多个 NVIDIA API Key 的调度管理：轮询 / 加权 / 失败自动切换。

用法：
    from key_manager import KeyManager
    km = KeyManager(keys=["nvapi-a", "nvapi-b"], strategy="round_robin")
    key = km.next()
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from typing import Callable, Sequence


class KeyManager:
    """管理一组 API Key 并按策略分发。

    strategies:
      - "round_robin" : 依次轮询
      - "random"      : 随机
      - "weighted"    : 加权随机采样，要求传入 (key, weight) 序列，weight >= 0
    """

    def __init__(
        self,
        keys: Sequence[str | tuple[str, int]],
        strategy: str = "round_robin",
    ) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._cooldown_until: dict[str, float] = {}

        if strategy == "weighted":
            self._weighted = self._parse_weighted(keys)
            self._keys = [k for k, _ in self._weighted]
        else:
            self._keys = [str(k) for k in keys]
            self._weighted = []

        self.strategy = strategy
        self._rr = itertools.cycle(self._keys)
        try:
            self._func: Callable[[], str] = {
                "round_robin": self._next_rr,
                "random": self._next_random,
                "weighted": self._next_weighted,
            }[strategy]
        except KeyError:
            raise ValueError(
                f"未知策略 {strategy!r}，可选：round_robin / random / weighted"
            ) from None

    @staticmethod
    def _parse_weighted(keys: Sequence[str | tuple[str, int]]) -> list[tuple[str, int]]:
        """校验 weighted 入参：必须是 (key, weight>=0) 序列，给出可读错误。"""
        weighted: list[tuple[str, int]] = []
        for entry in keys:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError(
                    "weighted 策略要求传入 (key, weight) 元组序列，"
                    f"例如 [('nvapi-a', 5), ('nvapi-b', 1)]；收到：{entry!r}"
                )
            key, weight = entry
            if not isinstance(weight, (int, float)) or weight < 0:
                raise ValueError(f"权重必须是非负数，收到 ({key!r}, {weight!r})")
            weighted.append((str(key), weight))
        return weighted

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def next(self) -> str:
        """返回下一个可用 Key（跳过冷却中/失败的）。"""
        with self._lock:
            for _ in range(len(self._keys) + 1):
                key = self._func()
                if self._available(key):
                    return key
            # 全部不可用时，仍然返回一个，交由上层报错
            return self._func()

    def _next_rr(self) -> str:
        return next(self._rr)

    def _next_random(self) -> str:
        return random.choice(self._keys)

    def _next_weighted(self) -> str:
        total = sum(w for _, w in self._weighted)
        if total <= 0:
            # 所有权重为 0 时退化为均匀随机，避免 random.uniform(0, 0) 恒选第一个
            return random.choice(self._keys)
        r = random.uniform(0, total)
        for key, w in self._weighted:
            r -= w
            if r <= 0:
                return key
        return self._keys[0]

    def _available(self, key: str) -> bool:
        now = time.time()
        if now < self._cooldown_until.get(key, 0.0):
            return False
        return True

    def report_failure(self, key: str, *, cooldown_seconds: float = 60.0) -> None:
        """某 Key 失败时调用，进入冷却并累加失败次数。"""
        with self._lock:
            self._failures[key] = self._failures.get(key, 0) + 1
            self._cooldown_until[key] = time.time() + cooldown_seconds

    def report_success(self, key: str) -> None:
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._failures)
