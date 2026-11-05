"""Per-upstream-account adaptive concurrency cap + optional RPM token bucket.

Models sharing one key (e.g. kimi-k2.6 and kimi-k3 on the same Moonshot
account) share one bucket, because the vendor enforces limits per account.

The concurrency window is AIMD: a 429 shrinks it (×0.7, floor 1), each success
grows it by ~1/window up to ``max_concurrency``. Excess callers queue locally
(up to the queue timeout) instead of hammering the vendor into more 429s.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional


def _int_env(name: str, default: int) -> int:
    try:
        return int((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default


class Busy(Exception):
    pass


class _Bucket:
    def __init__(self, max_concurrency: int, rpm: int):
        self.max_concurrency = max(1, max_concurrency)
        self.rpm = max(0, rpm)
        self.window = float(self.max_concurrency)
        self._cond = threading.Condition()
        self._tokens = float(self.rpm)
        self._stamp = time.monotonic()
        self.in_flight = 0
        self.waited = 0
        self.rejected = 0
        self.throttled = 0

    def _limit(self) -> int:
        return max(1, int(self.window))

    def _take_token(self, deadline: float) -> bool:
        if not self.rpm:
            return True
        rate = self.rpm / 60.0
        while True:
            with self._cond:
                now = time.monotonic()
                self._tokens = min(float(self.rpm), self._tokens + (now - self._stamp) * rate)
                self._stamp = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                need = (1.0 - self._tokens) / rate
            if now + need > deadline:
                return False
            time.sleep(min(need, 0.25))

    @contextmanager
    def slot(self, wait: float) -> Iterator[None]:
        deadline = time.monotonic() + max(0.0, wait)
        with self._cond:
            if self.in_flight >= self._limit():
                self.waited += 1
            while self.in_flight >= self._limit():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cond.wait(remaining):
                    if self.in_flight >= self._limit():
                        self.rejected += 1
                        raise Busy("concurrency")
            self.in_flight += 1
        try:
            if not self._take_token(deadline):
                with self._cond:
                    self.rejected += 1
                raise Busy("rpm")
            yield
        finally:
            with self._cond:
                self.in_flight -= 1
                self._cond.notify()

    def feedback(self, *, ok: bool, rate_limited: bool) -> None:
        with self._cond:
            if rate_limited:
                self.throttled += 1
                self.window = max(1.0, self.window * 0.7)
            elif ok and self.window < self.max_concurrency:
                self.window = min(float(self.max_concurrency), self.window + 1.0 / self.window)
                self._cond.notify_all()


_buckets: dict[str, _Bucket] = {}
_lock = threading.Lock()


def bucket(group: str, max_concurrency: Optional[int] = None, rpm: Optional[int] = None) -> _Bucket:
    b = _buckets.get(group)
    if b is not None:
        return b
    with _lock:
        b = _buckets.get(group)
        if b is None:
            b = _Bucket(
                max_concurrency if max_concurrency is not None else _int_env("LLM_MAX_CONCURRENCY", 8),
                rpm if rpm is not None else _int_env("LLM_RPM", 0),
            )
            _buckets[group] = b
    return b


def reset() -> None:
    with _lock:
        _buckets.clear()


def stats() -> dict[str, dict[str, float]]:
    return {
        g: {
            "max_concurrency": b.max_concurrency,
            "window": round(b.window, 2),
            "rpm": b.rpm,
            "in_flight": b.in_flight,
            "waited": b.waited,
            "rejected": b.rejected,
            "throttled_429": b.throttled,
        }
        for g, b in list(_buckets.items())
    }
