"""Baseline security response headers for the browser-facing app.

Kept deliberately conservative: same-origin scripts, self-hosted fonts, inline
styles (the UI sets a few styles from JS), data/blob images (favicons and the
food preview). HSTS is only sent on HTTPS so local HTTP development still works.
"""

from __future__ import annotations

from typing import Any

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "manifest-src 'self'; "
    "worker-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

STATIC_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", CSP.encode("latin-1")),
    (b"permissions-policy", b"geolocation=(), microphone=(), camera=(self)"),
)


class SecurityHeadersMiddleware:
    """Append headers to every HTTP response, including 4xx/5xx and 429."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        extra = list(STATIC_HEADERS)
        if scope.get("scheme") == "https":
            extra.append((b"strict-transport-security",
                          b"max-age=31536000; includeSubDomains"))

        async def wrapped(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {name.lower() for name, _ in headers}
                for name, value in extra:
                    if name not in present:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)
