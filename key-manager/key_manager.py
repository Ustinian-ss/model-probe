"""多个 NVIDIA API Key 的调度管理：轮询 / 加权 / 最少负载 + 滑动窗口限流 + 失败分级冷却。

用法：
    from key_manager import KeyManager
    km = KeyManager(keys=["nvapi-a", "nvapi-b"], strategy="round_robin")
    key = km.next()

设计对齐同机 nvapi-pool-gateway.mjs 的实战语义：
- 每把 Key 一个滑动窗口（默认 40 次/分钟，对应 NVIDIA 免费层）；
- 冷却按状态分级：401/403 长冷却、429 尊重 Retry-After、5xx 短冷却；
- least_loaded 策略“余量最大优先，并列时延迟 EMA 最小者优先”。
"""

from __future__ import annotations

import itertools
import random
import threading
import time
from collections.abc import Callable, Collection, Sequence


class KeyUnavailableError(RuntimeError):
    """没有任何 Key 可以立刻使用。

    调用方可根据 ``reason`` 决定排队等待还是直接报错：

    - ``"empty"``：Key 池为空；
    - ``"cooling"``：有 Key 还有窗口额度，但都在失败冷却中；
    - ``"rate_limited"``：所有 Key 本窗口的额度都用完了；
    - ``"excluded"``：调用方把候选 Key 全部排除（例如同一模型已经试过所有 Key）。

    ``retry_after`` 是“最早可能恢复可用”的秒数；冷却和限流同时存在时取更早恢复的那个。
    """

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        self.reason = reason
        self.retry_after = retry_after
        wait = "未知" if retry_after is None else f"{retry_after:.1f}s"
        super().__init__(f"没有可用 Key（{reason}，最早 {wait} 后恢复）")


def _coerce_retry_after(value: float | int | str | None) -> float | None:
    """把 Retry-After 归一化成非负秒数；无法解析时返回 None。"""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


class KeyManager:
    """管理一组 API Key 并按策略分发。

    strategies:
      - "round_robin"  : 依次轮询（默认）
      - "random"       : 随机
      - "weighted"     : 加权随机采样，要求传入 (key, weight) 序列，weight >= 0
      - "least_loaded" : 余量最大者优先；余量相同则延迟 EMA 最小者优先（别名 "smart"）

    限流：
      ``rpm_per_key`` 是每把 Key 的滑动窗口请求上限（默认 40，对应 NVIDIA 免费层）；
      ``rpm_per_account`` 可选，按账号再限一层（用 ``accounts`` 建立 key → 账号映射）。
      ``next()`` 只返回窗口内还有余量的 Key；全部不可用时抛 :class:`KeyUnavailableError`，
      并区分是“都在冷却”还是“额度用完”，附带可等待秒数。
    """

    STRATEGIES = ("round_robin", "random", "weighted", "least_loaded", "smart")

    def __init__(
        self,
        keys: Sequence[str | tuple[str, int]],
        strategy: str = "round_robin",
        *,
        rpm_per_key: int | None = 40,
        rpm_per_account: int | None = None,
        window_seconds: float = 60.0,
        accounts: dict[str, str] | None = None,
        default_cooldown_seconds: float = 60.0,
        auth_cooldown_seconds: float = 300.0,
        cooldown_429_seconds: float = 20.0,
        cooldown_5xx_seconds: float = 2.0,
        latency_ema_alpha: float = 0.3,
    ) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._total_failures: dict[str, int] = {}
        self._last_status: dict[str, int | None] = {}
        self._cooldown_until: dict[str, float] = {}
        self._latency_ema: dict[str, float] = {}
        self._hits: dict[str, list[float]] = {}
        self._account_hits: dict[str, list[float]] = {}
        self._accounts = dict(accounts or {})

        if strategy == "weighted":
            self._weighted = self._parse_weighted(keys)
            self._keys = [k for k, _ in self._weighted]
        else:
            self._keys = [str(k) for k in keys]
            self._weighted = []
        self._weight_map = {k: w for k, w in self._weighted}

        if window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")
        for name, cap in (("rpm_per_key", rpm_per_key), ("rpm_per_account", rpm_per_account)):
            if cap is not None and cap < 0:
                raise ValueError(f"{name} 必须是非负数或 None（不限流），收到 {cap!r}")

        self.strategy = strategy
        self.rpm_per_key = rpm_per_key
        self.rpm_per_account = rpm_per_account
        self.window_seconds = float(window_seconds)
        self.default_cooldown_seconds = float(default_cooldown_seconds)
        self.auth_cooldown_seconds = float(auth_cooldown_seconds)
        self.cooldown_429_seconds = float(cooldown_429_seconds)
        self.cooldown_5xx_seconds = float(cooldown_5xx_seconds)
        self.latency_ema_alpha = float(latency_ema_alpha)

        self._rr = itertools.cycle(self._keys)
        if strategy in ("least_loaded", "smart"):
            self._func: Callable[[list[str], float], str] = self._next_least_loaded
        else:
            try:
                self._func = {
                    "round_robin": self._next_rr,
                    "random": self._next_random,
                    "weighted": self._next_weighted,
                }[strategy]
            except KeyError:
                raise ValueError(
                    f"未知策略 {strategy!r}，可选：round_robin / random / weighted / least_loaded"
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

    # ---------- 只读信息 ----------

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def headroom(self, key: str) -> int | None:
        """返回该 Key 当前窗口的剩余次数；None 表示未启用限流。"""
        with self._lock:
            return self._headroom_locked(key, time.time())

    def stats(self) -> dict[str, dict[str, object]]:
        """每把 Key 的余量 / 失败数 / 最近状态 / 延迟 EMA（线程安全快照）。"""
        with self._lock:
            now = time.time()
            snapshot: dict[str, dict[str, object]] = {}
            for key in self._keys:
                hits = self._prune_locked(self._hits.get(key), now)
                snapshot[key] = {
                    "remaining": self._headroom_locked(key, now),
                    "used_in_window": len(hits),
                    "failures": self._failures.get(key, 0),
                    "total_failures": self._total_failures.get(key, 0),
                    "last_status": self._last_status.get(key),
                    "latency_ema_ms": self._latency_ema.get(key),
                    "cooldown_seconds": max(0.0, self._cooldown_until.get(key, 0.0) - now),
                }
            return snapshot

    # ---------- 取 Key ----------

    def next(self, *, exclude: Collection[str] | None = None, relax: bool = False) -> str:
        """返回一个窗口内还有余量、且不在冷却中的 Key。

        - ``exclude``：同一请求内“已经试过的 Key”，避免重试时又选到同一把；
        - ``relax=True``：忽略冷却（模型级故障转移时，冷却往往是上一个模型打出来的），
          但**仍然不放宽**窗口限流；
        - 全部不可用时抛 :class:`KeyUnavailableError`，不会静默返回已经超额的 Key。
        """
        with self._lock:
            now = time.time()
            candidates = self._candidates_locked(now, exclude, relax)
            if not candidates:
                reason, retry_after = self._unavailable_locked(now, exclude, relax)
                raise KeyUnavailableError(reason, retry_after)
            key = self._func(candidates, now)
            self._commit_locked(key, now)
            return key

    def _candidates_locked(
        self, now: float, exclude: Collection[str] | None, relax: bool
    ) -> list[str]:
        excluded = set(exclude) if exclude else set()
        candidates: list[str] = []
        for key in self._keys:
            if key in excluded:
                continue
            if not relax and now < self._cooldown_until.get(key, 0.0):
                continue
            free = self._headroom_locked(key, now)
            if free is not None and free <= 0:
                continue
            candidates.append(key)
        return candidates

    def _unavailable_locked(
        self, now: float, exclude: Collection[str] | None, relax: bool
    ) -> tuple[str, float | None]:
        """区分“没 Key / 都在冷却 / 都没额度”，并给出最早可恢复秒数。"""
        if not self._keys:
            return "empty", None
        excluded = set(exclude) if exclude else set()
        usable = [key for key in self._keys if key not in excluded]
        if not usable:
            return "excluded", None

        cooling_waits: list[float] = []
        quota_waits: list[float] = []
        for key in usable:
            free = self._headroom_locked(key, now)
            cooling_left = max(0.0, self._cooldown_until.get(key, 0.0) - now)
            if free is None or free > 0:
                if not relax and cooling_left > 0:
                    cooling_waits.append(cooling_left)
                else:
                    # 有额度又没冷却的 Key 本应已被 _candidates_locked 选中；
                    # 走到这里说明只是被 exclude 过滤，按 0 秒处理即可。
                    cooling_waits.append(0.0)
            else:
                quota_waits.append(self._quota_wait_locked(key, now))

        options: list[tuple[str, float]] = []
        if cooling_waits:
            options.append(("cooling", min(cooling_waits)))
        if quota_waits:
            options.append(("rate_limited", min(quota_waits)))
        if not options:
            return "unavailable", None
        return min(options, key=lambda item: item[1])

    def _quota_wait_locked(self, key: str, now: float) -> float:
        """额度用完时，返回该 Key 最早恢复一格余量所需的秒数。"""
        waits: list[float] = []
        if self.rpm_per_key is not None:
            hits = self._prune_locked(self._hits.get(key), now)
            if len(hits) >= self.rpm_per_key:
                waits.append(max(0.0, hits[0] + self.window_seconds - now))
        account = self._accounts.get(key)
        if self.rpm_per_account is not None and account is not None:
            hits = self._prune_locked(self._account_hits.get(account), now)
            if len(hits) >= self.rpm_per_account:
                waits.append(max(0.0, hits[0] + self.window_seconds - now))
        # 账号和 Key 两个维度都满了的话，必须等两个窗口都腾出一格
        return max(waits) if waits else 0.0

    # ---------- 策略实现 ----------

    def _next_rr(self, candidates: list[str], now: float) -> str:
        allowed = set(candidates)
        for _ in range(len(self._keys)):
            key = next(self._rr)
            if key in allowed:
                return key
        return candidates[0]

    def _next_random(self, candidates: list[str], now: float) -> str:
        return random.choice(candidates)

    def _next_weighted(self, candidates: list[str], now: float) -> str:
        total = sum(self._weight_map.get(key, 0) for key in candidates)
        if total <= 0:
            # 候选权重全为 0 时退化为均匀随机，避免 random.uniform(0, 0) 恒选第一个
            return random.choice(candidates)
        r = random.uniform(0, total)
        for key in candidates:
            r -= self._weight_map.get(key, 0)
            if r <= 0:
                return key
        return candidates[-1]

    def _next_least_loaded(self, candidates: list[str], now: float) -> str:
        """余量最大优先；并列时延迟 EMA 最小者优先（“哪个快、哪个有空位就用哪个”）。"""

        def sort_key(key: str) -> tuple[float, float, int, float]:
            free = self._headroom_locked(key, now)
            free_rank = float("inf") if free is None else float(free)
            ema = self._latency_ema.get(key)
            ema_rank = float("inf") if ema is None else float(ema)
            return (
                -free_rank,
                ema_rank,
                len(self._hits.get(key, ())),
                self._cooldown_until.get(key, 0.0),
            )

        return min(candidates, key=sort_key)

    # ---------- 记账 ----------

    def _headroom_locked(self, key: str, now: float) -> int | None:
        frees: list[int] = []
        if self.rpm_per_key is not None:
            used = len(self._prune_locked(self._hits.get(key), now))
            frees.append(self.rpm_per_key - used)
        account = self._accounts.get(key)
        if self.rpm_per_account is not None and account is not None:
            used = len(self._prune_locked(self._account_hits.get(account), now))
            frees.append(self.rpm_per_account - used)
        if not frees:
            return None
        return max(0, min(frees))

    def _prune_locked(self, hits: list[float] | None, now: float) -> list[float]:
        """原地剪掉滑动窗口外的旧记录，并返回该列表。"""
        if not hits:
            return []
        cut = now - self.window_seconds
        index = 0
        while index < len(hits) and hits[index] <= cut:
            index += 1
        if index:
            del hits[:index]
        return hits

    def _commit_locked(self, key: str, now: float) -> None:
        self._hits.setdefault(key, []).append(now)
        account = self._accounts.get(key)
        if account is not None:
            self._account_hits.setdefault(account, []).append(now)

    # ---------- 成功 / 失败上报 ----------

    @staticmethod
    def _coerce_status(status: int | str | None) -> int | None:
        if status is None:
            return None
        try:
            return int(status)
        except (TypeError, ValueError):
            return None

    def _cooldown_for(
        self,
        status: int | None,
        retry_after: float | int | str | None,
        cooldown_seconds: float | None,
    ) -> float:
        """按状态分级算冷却：显式 cooldown_seconds 永远优先。"""
        if cooldown_seconds is not None:
            return max(0.0, float(cooldown_seconds))
        if status in (401, 403):
            return max(0.0, self.auth_cooldown_seconds)
        if status == 429:
            wait = _coerce_retry_after(retry_after)
            if wait is not None and wait > 0:
                return wait
            return max(0.0, self.cooldown_429_seconds)
        if status is not None and 500 <= status < 600:
            return max(0.0, self.cooldown_5xx_seconds)
        if status is not None and 400 <= status < 500:
            # 400/404/422 等参数/模型类错误不是 Key 的问题，不冷却
            return 0.0
        return max(0.0, self.default_cooldown_seconds)

    def report_failure(
        self,
        key: str,
        *,
        status: int | str | None = None,
        retry_after: float | int | str | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        """某 Key 失败时调用，按状态分级进入冷却并累加失败次数。

        - ``status=401/403``：长冷却（默认 300s，``auth_cooldown_seconds`` 可调），不永久拉黑；
        - ``status=429``：优先使用 ``retry_after``，否则默认 20s；
        - ``status=5xx``：短冷却（默认 2s），避免整个池因模型级瞬时故障停摆；
        - 旧调用 ``report_failure(key, cooldown_seconds=60.0)`` 保持可用。
        """
        with self._lock:
            normalized = self._coerce_status(status)
            self._failures[key] = self._failures.get(key, 0) + 1
            self._total_failures[key] = self._total_failures.get(key, 0) + 1
            self._last_status[key] = normalized
            cooldown = self._cooldown_for(normalized, retry_after, cooldown_seconds)
            self._cooldown_until[key] = time.time() + cooldown

    def report_success(self, key: str, *, latency_ms: float | None = None) -> None:
        """某 Key 成功时调用：清除冷却/连续失败计数，并更新延迟 EMA。"""
        with self._lock:
            self._cooldown_until.pop(key, None)
            self._failures.pop(key, None)
            self._last_status[key] = 200
            if latency_ms is not None:
                latency = float(latency_ms)
                previous = self._latency_ema.get(key)
                if previous is None:
                    self._latency_ema[key] = latency
                else:
                    alpha = self.latency_ema_alpha
                    self._latency_ema[key] = previous * (1 - alpha) + latency * alpha
