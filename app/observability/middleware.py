"""ASGI middleware: one root observation per API request + ``X-Trace-Id`` response header."""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from app.observability import tracing

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

_TRACED_PREFIXES = ("/api/",)
_SKIP = ("/api/admin/", "/api/traces/", "/api/feedback")


def _route_name(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None) or scope.get("path") or ""
    return f"{scope.get('method', 'GET')} {path}"


class TraceMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path") or ""
        if scope.get("type") != "http" or not path.startswith(_TRACED_PREFIXES) or path.startswith(_SKIP):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        t0 = time.perf_counter()
        status = {"code": 0}
        with tracing.request_trace(f"{method} {path}", method=method, path=path) as root:
            trace_id = root.trace_id

            async def _send(message: dict[str, Any]) -> None:
                if message.get("type") == "http.response.start":
                    status["code"] = int(message.get("status") or 0)
                    if trace_id:
                        headers = list(message.get("headers") or [])
                        headers.append((b"x-trace-id", trace_id.encode()))
                        message = {**message, "headers": headers}
                await send(message)

            try:
                await self.app(scope, receive, _send)
            finally:
                try:
                    root.attr("langfuse.trace.name", _route_name(scope))
                    root.attr("http.route", getattr(scope.get("route"), "path", "") or path)
                    root.attr("http.response.status_code", status["code"])
                    root.meta(latency_ms=round((time.perf_counter() - t0) * 1000.0, 1), status=status["code"])
                    if status["code"] >= 500:
                        root.level("ERROR", f"HTTP {status['code']}")
                except Exception:
                    pass
