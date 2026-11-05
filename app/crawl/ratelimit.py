"""Process-wide domain token buckets and concurrent network request bound."""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager


class RateLimiter:
    def __init__(self, concurrency=4, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._buckets = {}
        self._slots = threading.BoundedSemaphore(concurrency)

    def acquire(self, domain, requests, seconds):
        with self._lock:
            now = self.clock()
            # Keep the most restrictive rate seen for this domain, across adapters.
            tokens, last, capacity, interval = self._buckets.get(domain, (requests, now, requests, seconds))
            capacity = min(capacity, requests)
            interval = max(interval / capacity, seconds / requests) * capacity
            tokens = min(capacity, tokens + max(0, now - last) * capacity / interval)
            start = max(now, last)
            delay = max(0, (1 - tokens) * interval / capacity)
            reserved = start + delay
            self._buckets[domain] = (max(0, tokens - 1), reserved, capacity, interval)
        wait = reserved - now
        if wait > 0:
            self.sleep(wait)

    @contextmanager
    def slot(self, domain, policy):
        # Reserve rate while holding the slot: queued callers cannot burst later.
        with self._slots:
            self.acquire(domain, policy.rate_requests, policy.rate_seconds)
            yield
