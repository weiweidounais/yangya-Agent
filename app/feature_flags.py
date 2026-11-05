"""Runtime feature flags (env-driven). Defaults are safe for production."""

from __future__ import annotations

import os
import hashlib


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def memory_vector_enabled() -> bool:
    """Kill switch plus stable rollout, using only the trusted user scope."""
    if not _truthy(os.getenv("MEMORY_VECTOR_ENABLED")):
        return False
    from app.data_access import current_user, IdentityRequired

    uid = current_user()
    if uid is None:
        raise IdentityRequired("Vector memory requires a user scope")
    allowed = {value.strip() for value in os.getenv("MEMORY_VECTOR_ALLOW_USERS", "").split(",")
               if value.strip()}
    if uid in allowed:
        return True
    try:
        percent = int(os.getenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "0"))
    except ValueError:
        return False
    if not 0 <= percent <= 100:
        return False
    bucket = int.from_bytes(hashlib.sha256(uid.encode("utf-8")).digest()[:8], "big") % 10000
    return bucket < percent * 100
