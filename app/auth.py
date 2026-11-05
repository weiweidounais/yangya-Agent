"""Signed user tokens for endpoints that must not trust a body ``user_id``.

Token: ``v1.<b64url(user_id)>.<exp>.<hex hmac-sha256>`` keyed by ``AUTH_SECRET``.
Fail closed: without ``AUTH_SECRET`` protected endpoints answer 503.
Mint tokens server-side (login / channel binding) with ``mint_user_token``,
or for ops: ``python -m app.auth <user_id> [ttl_seconds]``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sys
import time
from typing import Optional

from fastapi import HTTPException, Request

DEFAULT_TTL_SECONDS = 30 * 24 * 3600


def _secret() -> bytes:
    return (os.getenv("AUTH_SECRET") or "").strip().encode("utf-8")


def auth_configured() -> bool:
    return bool(_secret())


def _sign(body: str) -> str:
    return hmac.new(_secret(), body.encode("utf-8"), hashlib.sha256).hexdigest()


def mint_user_token(user_id: str, ttl_seconds: Optional[int] = None) -> str:
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id required")
    if not auth_configured():
        raise RuntimeError("AUTH_SECRET not set")
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    exp = int(time.time()) + max(1, ttl)
    enc = base64.urlsafe_b64encode(uid.encode("utf-8")).decode("ascii").rstrip("=")
    body = f"v1.{enc}.{exp}"
    return f"{body}.{_sign(body)}"


def verify_user_token(token: str) -> Optional[str]:
    """Return the user_id for a valid, unexpired token; else None."""
    if not auth_configured():
        return None
    parts = (token or "").strip().split(".")
    if len(parts) != 4 or parts[0] != "v1":
        return None
    body = ".".join(parts[:3])
    if not hmac.compare_digest(_sign(body), parts[3]):
        return None
    try:
        if int(parts[2]) < int(time.time()):
            return None
        pad = "=" * (-len(parts[1]) % 4)
        uid = base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError):
        return None
    return uid or None


# HttpOnly browser session cookie minted by POST /api/session (see app.session_identity).
SESSION_COOKIE = "ja_session"


def require_user(request: Request) -> str:
    """FastAPI dependency: authenticated user_id from ``Authorization: Bearer``,
    ``X-User-Token`` or the signed session cookie. Never falls back to a
    default/demo account."""
    if not auth_configured():
        raise HTTPException(
            status_code=503,
            detail={"code": "auth_not_configured", "message": "AUTH_SECRET 未配置，接口已关闭"},
        )
    raw = request.headers.get("authorization") or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    token = token or (request.headers.get("x-user-token") or "").strip()
    token = token or (request.cookies.get(SESSION_COOKIE) or "").strip()
    uid = verify_user_token(token) if token else None
    if not uid:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthenticated", "message": "需要有效的用户令牌"},
        )
    return uid


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) < 2:
        print("usage: python -m app.auth <user_id> [ttl_seconds]", file=sys.stderr)
        raise SystemExit(2)
    print(mint_user_token(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else None))
