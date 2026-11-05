"""Per-IP / per-token HTTP rate limiting for the public API.

Disabled by default so local development and the test suite are unaffected;
set ``API_RATE_LIMIT_ENABLED=1`` (see ``.env.example``) for any deployment that
is reachable from the internet. Counters are process-local: behind multiple
workers or instances, treat the configured limit as per worker.

Only ``/api/*`` is counted. Health checks and static assets are exempt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from typing import Any, Optional

_HEADER_TOKEN = (b"authorization", b"x-user-token", b"cookie")


def _env_int(name: str, default: int, low: int = 1, high: int = 100_000) -> int:
    try:
        value = int(os.getenv(name) or default)
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def enabled() -> bool:
    return (os.getenv("API_RATE_LIMIT_ENABLED") or "0").strip().lower() in {"1", "true", "yes", "on"}


def limits() -> tuple[int, int, float]:
    window = float(_env_int("API_RATE_LIMIT_WINDOW_SECONDS", 60, 5, 3600))
    return (_env_int("API_RATE_LIMIT_PER_MINUTE", 300, 5, 100_000),
            _env_int("API_RATE_LIMIT_USER_PER_MINUTE", 120, 5, 100_000),
            window)


class _Bucket:
    __slots__ = ("count", "reset_at")

    def __init__(self, reset_at: float) -> None:
        self.count = 0
        self.reset_at = reset_at


_lock = threading.Lock()
_buckets: dict[str, _Bucket] = {}
_calls = 0


def reset() -> None:
    """Test helper: clear all counters."""
    global _calls
    with _lock:
        _buckets.clear()
        _calls = 0


def _token_key(scope: dict[str, Any]) -> str:
    raw = b""
    for name, value in scope.get("headers") or []:
        if name.lower() in _HEADER_TOKEN:
            raw += value[:512]
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()[:20]


def _client_ip(scope: dict[str, Any]) -> str:
    # Never trust a forwarding header unless the operator explicitly says a
    # proxy in front of this service overwrites it.
    header = (os.getenv("API_TRUSTED_PROXY_HEADER") or "").strip().lower()
    if header:
        want = header.encode("latin-1")
        for name, value in scope.get("headers") or []:
            if name.lower() == want:
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first[:64]
    client = scope.get("client")
    return str(client[0])[:64] if client else "unknown"


def _hit(key: str, limit: int, window: float) -> tuple[bool, int, int]:
    """Return (allowed, remaining, retry_after_seconds)."""
    global _calls
    now = time.monotonic()
    with _lock:
        _calls += 1
        if _calls % 2048 == 0 and len(_buckets) > 4096:
            for stale in [k for k, b in _buckets.items() if b.reset_at <= now]:
                _buckets.pop(stale, None)
        bucket = _buckets.get(key)
        if bucket is None or bucket.reset_at <= now:
            bucket = _Bucket(now + window)
            _buckets[key] = bucket
        bucket.count += 1
        if bucket.count > limit:
            return False, 0, max(1, math.ceil(bucket.reset_at - now))
        return True, max(0, limit - bucket.count), 0


async def _deny(scope: dict[str, Any], receive: Any, send: Any, retry_after: int) -> None:
    body = json.dumps(
        {"detail": {"code": "rate_limited", "message": "请求太频繁，请稍后再试"}},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode()),
        (b"retry-after", str(retry_after).encode()),
        (b"cache-control", b"no-store"),
    ]
    if scope.get("method") == "HEAD":
        body = b""
    await send({"type": "http.response.start", "status": 429, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RateLimitMiddleware:
    """Pure-ASGI middleware: count each /api request against IP and token buckets."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (scope.get("type") != "http"
                or not enabled()
                or scope.get("method") == "OPTIONS"
                or not str(scope.get("path") or "").startswith("/api/")):
            await self.app(scope, receive, send)
            return
        per_ip, per_user, window = limits()
        allowed, _, retry = _hit("ip:" + _client_ip(scope), per_ip, window)
        if allowed:
            token = _token_key(scope)
            if token:
                allowed, _, retry = _hit("user:" + token, per_user, window)
        if not allowed:
            await _deny(scope, receive, send, retry)
            return
        await self.app(scope, receive, send)
