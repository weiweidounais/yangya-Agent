"""Channel env switches. Every channel defaults to OFF."""

from __future__ import annotations

import os

RESERVED_USER_IDS = frozenset({"", "demo", "anonymous", "guest"})


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def feishu_enabled() -> bool:
    """``FEISHU_ENABLED=1`` (legacy alias ``FEISHU_WS_ENABLED=1``). Credentials alone do not enable."""
    return _flag("FEISHU_ENABLED") or _flag("FEISHU_WS_ENABLED")


def wechat_enabled() -> bool:
    return _flag("OPENCLAW_WECHAT_ENABLED")


def public_base_url() -> str:
    """Absolute origin used in channel links (bind page / privacy sandbox)."""
    raw = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if raw:
        return raw
    port = (os.getenv("PORT") or "8787").strip()
    return f"http://127.0.0.1:{port}"


def bind_link_ttl() -> int:
    return int(os.getenv("CHANNEL_BIND_TTL_SECONDS") or "900")


def sandbox_link_ttl() -> int:
    return int(os.getenv("CHANNEL_SANDBOX_TTL_SECONDS") or "1800")


def admin_token() -> str:
    return (os.getenv("CHANNEL_ADMIN_TOKEN") or "").strip()


def is_reserved_user_id(user_id: str | None) -> bool:
    return str(user_id or "").strip().lower() in RESERVED_USER_IDS
