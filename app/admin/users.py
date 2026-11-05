"""Read-only user snapshots and audited access flags."""
import json
from datetime import timezone

from fastapi import HTTPException
from sqlalchemy import select

from app import db
from app.admin.models import AdminUserFlag, now
from app.admin.store import change, encoded, session, sha
from app.data_access.memory_facts import MemoryFact
from app.agent.memory.redact import display_text, is_secret_key
from app.agent.harness.redact import is_secret_field, mask_secrets


def safe_value(value):
    if isinstance(value, dict):
        return {k: safe_value(v) for k, v in value.items()
                if not (is_secret_key(k) or is_secret_field(k))}
    if isinstance(value, list):
        return [safe_value(v) for v in value]
    if isinstance(value, str):
        return display_text(mask_secrets(value)[0])
    return value


def profile_data(row):
    try:
        value = json.loads(row.profile_json, parse_constant=lambda _: None)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def display_name(profile):
    contact = profile.get("contact")
    return safe_value(str(profile.get("name") or (contact.get("name") if isinstance(contact, dict) else "") or ""))


def list_users(q="", disabled=None, limit=50, offset=0):
    with session() as s:
        # Profiles are JSON blobs on both supported databases. Decode only for
        # display-name matching; never search or return private profile bodies.
        rows = s.execute(select(db.UserProfile, AdminUserFlag.disabled_at).outerjoin(
            AdminUserFlag, AdminUserFlag.user_id == db.UserProfile.user_id
        ).order_by(db.UserProfile.user_id)).all()
        items = []
        for row, disabled_at in rows:
            name = display_name(profile_data(row))
            if q.casefold() not in row.user_id.casefold() and q.casefold() not in name.casefold():
                continue
            if disabled is not None and bool(disabled_at) != disabled:
                continue
            items.append({"user_id": row.user_id, "display_name": name, "created_at": None,
                          "last_active_at": None, "updated_at": row.updated_at, "disabled": bool(disabled_at),
                          "disabled_at": disabled_at})
        return {"total": len(items), "items": items[offset:offset + limit]}


_PROFILE_FIELDS = {"name", "contact", "city", "tz", "occupation", "interests", "preferences", "facts"}


def detail(user_id, reveal=False):
    with session() as s:
        row = s.get(db.UserProfile, user_id)
        if row is None:
            raise HTTPException(404, "用户不存在")
        profile = profile_data(row)
        flag = s.get(AdminUserFlag, user_id)
        facts = [{"id": f.id, "kind": f.kind, "value": safe_value(f.text) if reveal else {"sha": sha(f.text), "chars": len(f.text)},
                  "updated_at": f.updated_at} for f in s.scalars(select(MemoryFact).where(
                      MemoryFact.user_id == user_id).order_by(MemoryFact.id))]
        # Legacy facts are held inside the profile JSON as well.
        for key in ("facts", "memory_facts", "memory", "known_facts"):
            if key in profile:
                value = profile[key]
                facts.append({"kind": key, "value": safe_value(value) if reveal else {"sha": sha(value), "chars": len(value) if isinstance(value, str) else len(encoded(value))}})
        return {"user_id": user_id, "display_name": display_name(profile),
                "profile": safe_value({k: v for k, v in profile.items() if k in _PROFILE_FIELDS}),
                "memory_facts": facts, "revealed": reveal, "disabled": bool(flag and flag.disabled_at),
                "disabled_at": flag.disabled_at if flag else None,
                "usage": {"available": False}}


def set_disabled(user_id, disabled, principal):
    with session(write=True) as s:
        if s.get(db.UserProfile, user_id) is None:
            raise HTTPException(404, "用户不存在")
        flag = s.get(AdminUserFlag, user_id)
        if bool(flag and flag.disabled_at) == disabled:
            return {"user_id": user_id, "disabled": disabled}
        before = flag.disabled_at.replace(tzinfo=timezone.utc).isoformat() if flag and flag.disabled_at else None
        if flag is None:
            flag = AdminUserFlag(user_id=user_id)
            s.add(flag)
        flag.disabled_at = now() if disabled else None
        change(s, principal, "user", user_id, before, flag.disabled_at.isoformat() if flag.disabled_at else None)
    return {"user_id": user_id, "disabled": disabled}


def check_enabled(user_id):
    with session() as s:
        flag = s.get(AdminUserFlag, user_id)
        if flag and flag.disabled_at:
            raise HTTPException(403, detail={"code": "user_disabled", "message": "此账号已禁用"})
