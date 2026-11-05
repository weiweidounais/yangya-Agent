"""Encrypted per-user vault for credentials / PII collected through the secret sandbox.

Values are Fernet-encrypted with ``SECRETS_FERNET_KEY`` and are never returned by
any HTTP API, never written to chat history, checkpoints, memory, LLM prompts or
logs. Callers outside this module only ever see masked previews.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, ENGINE
from app.data_access.session import SessionLocal

MAX_VALUE_CHARS = 8192

_LOGIN_FIELDS: list[dict[str, Any]] = [
    {"name": "account", "label": "账号（手机号 / 邮箱）", "input": "text", "autocomplete": "off"},
    {"name": "password", "label": "密码", "input": "password", "autocomplete": "new-password"},
    {
        "name": "cookie",
        "label": "Cookie（可选，与密码二选一）",
        "input": "password",
        "autocomplete": "off",
        "optional": True,
    },
]

CATALOG: dict[str, dict[str, Any]] = {
    "account_login": {
        "platform": "账号密码",
        "label": "账号登录信息",
        "kind": "login",
        "fields": [
            {"name": "platform_name", "label": "网站 / 应用名称", "input": "text", "autocomplete": "off"},
            *_LOGIN_FIELDS,
        ],
        "aliases": ("账号密码", "账号", "登录信息", "密码", "login", "account", "other"),
    },
    "phone": {
        "platform": "手机号",
        "label": "手机号",
        "kind": "phone",
        "fields": [{"name": "phone", "label": "手机号", "input": "password", "autocomplete": "off"}],
        "aliases": ("手机号", "手机", "电话", "phone", "mobile"),
    },
    "id_card": {
        "platform": "身份证",
        "label": "身份证号",
        "kind": "id_card",
        "fields": [{"name": "id_number", "label": "身份证号", "input": "password", "autocomplete": "off"}],
        "aliases": ("身份证", "身份证号", "id_card", "idcard"),
    },
}

_PHONE_RE = re.compile(r"^(?:\+?86)?1[3-9]\d{9}$")
_ID_RE = re.compile(r"^[1-9]\d{16}[\dXx]$")


class VaultUnavailable(RuntimeError):
    """SECRETS_FERNET_KEY missing or invalid — refuse to store anything."""


class SecretValidationError(ValueError):
    """Bad input. Messages are user-facing and never echo submitted values."""


class UserSecret(Base):
    __tablename__ = "user_secrets"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_secret_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    key: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    masked_json: Mapped[str] = mapped_column(Text, default="[]")
    purpose: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def _ensure_table() -> None:
    UserSecret.__table__.create(bind=ENGINE, checkfirst=True)


def _fernet():
    from cryptography.fernet import Fernet, MultiFernet

    raw = (os.getenv("SECRETS_FERNET_KEY") or "").strip()
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        raise VaultUnavailable("SECRETS_FERNET_KEY not set")
    try:
        # First key encrypts; the rest still decrypt (rotation).
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except Exception as exc:
        raise VaultUnavailable("SECRETS_FERNET_KEY invalid") from exc


def is_configured() -> bool:
    try:
        _fernet()
        return True
    except VaultUnavailable:
        return False


def resolve_key(platform: str = "", kind: str = "") -> Optional[str]:
    """Map an LLM / heuristic platform name onto a catalog key (None if unknown)."""
    p = str(platform or "").strip().lower()
    k = str(kind or "").strip().lower()
    if p in CATALOG:
        return p
    if k in ("phone", "id_card"):
        return k
    if not p:
        return None
    for key, spec in CATALOG.items():
        if any(alias.lower() in p for alias in spec["aliases"]):
            return key
    return None


def spec_for(key: str) -> dict[str, Any]:
    spec = CATALOG.get(key)
    if not spec:
        raise SecretValidationError("不支持保存这类信息")
    return spec


def public_fields(key: str) -> list[dict[str, Any]]:
    return [dict(f) for f in spec_for(key)["fields"]]


def _clean_values(key: str, values: Any) -> dict[str, str]:
    spec = spec_for(key)
    if not isinstance(values, dict):
        raise SecretValidationError("没有收到要保存的内容")
    allowed = {f["name"] for f in spec["fields"]}
    out: dict[str, str] = {}
    for name, raw in values.items():
        if name not in allowed or raw is None:
            continue
        if not isinstance(raw, str):
            raise SecretValidationError("内容格式不对，请重新填写")
        v = raw.strip()
        if len(v) > MAX_VALUE_CHARS:
            raise SecretValidationError("内容太长了，请检查后再保存")
        if v:
            out[name] = v

    kind = spec["kind"]
    if kind == "login":
        if not (out.get("cookie") or (out.get("account") and out.get("password"))):
            raise SecretValidationError("请填写账号和密码，或者只填 Cookie")
        if key == "account_login" and not out.get("platform_name"):
            raise SecretValidationError("请写上是哪个平台")
    elif kind == "phone":
        phone = re.sub(r"[\s-]", "", out.get("phone", ""))
        if not _PHONE_RE.match(phone):
            raise SecretValidationError("手机号格式不对，请输入 11 位手机号")
        out["phone"] = phone
    elif kind == "id_card":
        idn = out.get("id_number", "").replace(" ", "")
        if not _ID_RE.match(idn):
            raise SecretValidationError("身份证号格式不对，请输入 18 位身份证号")
        out["id_number"] = idn.upper()
    return out


def _mask_account(v: str) -> str:
    digits = re.sub(r"\D", "", v)
    if _PHONE_RE.match(digits):
        return f"{digits[-11:-8]}****{digits[-4:]}"
    if "@" in v:
        name, _, domain = v.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{v[:2]}***" if len(v) > 3 else "***"


def _masked(key: str, values: dict[str, str]) -> list[dict[str, str]]:
    labels = {f["name"]: f["label"] for f in spec_for(key)["fields"]}
    out: list[dict[str, str]] = []
    for name, v in values.items():
        if name == "platform_name":
            preview = v[:20]
        elif name == "account":
            preview = _mask_account(v)
        elif name == "phone":
            preview = f"{v[:3]}****{v[-4:]}"
        elif name == "id_number":
            preview = f"{v[:3]}{'*' * 11}{v[-4:]}"
        else:
            preview = "已保存"
        out.append({"name": name, "label": labels.get(name, name), "preview": preview})
    return out


def _public(row: UserSecret) -> dict[str, Any]:
    spec = CATALOG.get(row.key, {})
    try:
        masked = json.loads(row.masked_json or "[]")
    except json.JSONDecodeError:
        masked = []
    return {
        "key": row.key,
        "platform": spec.get("platform", row.key),
        "label": spec.get("label", row.key),
        "purpose": row.purpose or "",
        "masked": masked,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _encrypt(user_id: str, key: str, values: dict[str, str]) -> str:
    blob = json.dumps({"u": user_id, "k": key, "v": values}, ensure_ascii=False)
    return _fernet().encrypt(blob.encode("utf-8")).decode("ascii")


def save_secret(user_id: str, key: str, values: Any, *, purpose: str = "") -> dict[str, Any]:
    uid = str(user_id or "").strip()
    if not uid:
        raise SecretValidationError("缺少账号")
    clean = _clean_values(key, values)
    token = _encrypt(uid, key, clean)
    masked = json.dumps(_masked(key, clean), ensure_ascii=False)
    _ensure_table()
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        row = session.execute(
            select(UserSecret).where(UserSecret.user_id == uid, UserSecret.key == key)
        ).scalar_one_or_none()
        if row is None:
            row = UserSecret(user_id=uid, key=key, created_at=now)
            session.add(row)
        row.ciphertext = token
        row.masked_json = masked
        row.purpose = str(purpose or "")[:200]
        row.updated_at = now
        session.commit()
        session.refresh(row)
        return _public(row)


def list_secrets(user_id: str) -> list[dict[str, Any]]:
    uid = str(user_id or "").strip()
    if not uid:
        return []
    _ensure_table()
    with SessionLocal() as session:
        rows = session.execute(
            select(UserSecret).where(UserSecret.user_id == uid).order_by(UserSecret.updated_at.desc())
        ).scalars().all()
        return [_public(r) for r in rows]


def saved_labels(user_id: str) -> list[str]:
    try:
        return [item["label"] for item in list_secrets(user_id)]
    except Exception:
        return []


def delete_secret(user_id: str, key: str) -> bool:
    uid = str(user_id or "").strip()
    if not uid:
        return False
    _ensure_table()
    with SessionLocal() as session:
        row = session.execute(
            select(UserSecret).where(UserSecret.user_id == uid, UserSecret.key == key)
        ).scalar_one_or_none()
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def load_secret_values(user_id: str, key: str) -> Optional[dict[str, str]]:
    """Decrypt for server-side automation only. Never return this over HTTP or into a prompt."""
    from cryptography.fernet import InvalidToken

    uid = str(user_id or "").strip()
    if not uid:
        return None
    _ensure_table()
    with SessionLocal() as session:
        row = session.execute(
            select(UserSecret).where(UserSecret.user_id == uid, UserSecret.key == key)
        ).scalar_one_or_none()
        if row is None:
            return None
        token = row.ciphertext
    try:
        blob = json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError):
        return None
    # Ciphertext is bound to its owner and key: a row copied to another user does not decrypt.
    if blob.get("u") != uid or blob.get("k") != key:
        return None
    values = blob.get("v")
    return dict(values) if isinstance(values, dict) else None
