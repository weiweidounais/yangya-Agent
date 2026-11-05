"""Admin auth for harness endpoints (traces, audit, feedback → dataset). Fails closed.

Token sources (any combination; each token maps to a named principal + scopes):

- ``HARNESS_ADMIN_TOKENS`` — ``name:token[:scope|scope]`` entries separated by
  commas or newlines, e.g. ``alice:Xy..:traces:read|audit:read,bob:Zq..``.
- ``admin_tokens`` table — managed with ``scripts/admin_tokens.py``
  (create / list / revoke). Only the SHA-256 of a token is stored.
- ``HARNESS_ADMIN_TOKEN`` / ``ADMIN_TOKEN`` — shared tokens, principal ``legacy``.

No token configured anywhere → endpoints return 503 (disabled), never open.
Clients send ``Authorization: Bearer <token>`` or ``X-Admin-Token: <token>``.

Every admin request — allowed, denied or disabled — is written to
``admin_audit`` (principal, scope, method, path, pseudonymised query, client, status).
An allowed request whose audit row cannot be written is refused (503).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, init_db
from app.data_access import system_scope
from app.data_access.session import SessionLocal


log = logging.getLogger("harness.admin")

_MIN_TOKEN_LEN = 16
ALL_SCOPES = (
    "traces:read", "audit:read", "feedback:write", "users:read", "users:write",
    "prompts:read", "prompts:write", "prompts:publish",
    "routes:read", "routes:write", "routes:publish",
)


class AdminToken(Base):
    __tablename__ = "admin_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[str] = mapped_column(String(256), default="*")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AdminAudit(Base):
    __tablename__ = "admin_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    principal: Mapped[str] = mapped_column(String(64), index=True, default="")
    source: Mapped[str] = mapped_column(String(16), default="")
    outcome: Mapped[str] = mapped_column(String(16), index=True, default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    scope: Mapped[str] = mapped_column(String(32), default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(256), default="")
    query: Mapped[str] = mapped_column(Text, default="")
    client: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(256), default="")
    token_fp: Mapped[str] = mapped_column(String(16), default="")


@dataclass(frozen=True)
class AdminPrincipal:
    name: str
    source: str  # env | db | legacy
    scopes: frozenset[str] = field(default_factory=frozenset)
    token_id: Optional[int] = None

    def allows(self, scope: str) -> bool:
        return "*" in self.scopes or scope in self.scopes


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _scopes(raw: str) -> frozenset[str]:
    vals = {s.strip() for s in re.split(r"[|\s]+", raw or "") if s.strip()}
    return frozenset(vals or {"*"})


def admin_token() -> str:
    return (os.getenv("HARNESS_ADMIN_TOKEN") or "").strip() or (os.getenv("ADMIN_TOKEN") or "").strip()


def _legacy_tokens() -> list[str]:
    return [value for name in ("HARNESS_ADMIN_TOKEN", "ADMIN_TOKEN")
            if len(value := (os.getenv(name) or "").strip()) >= _MIN_TOKEN_LEN]


def _env_tokens() -> list[tuple[str, str, frozenset[str]]]:
    out = []
    for entry in re.split(r"[,\n]+", os.getenv("HARNESS_ADMIN_TOKENS") or ""):
        parts = entry.strip().split(":", 2)
        if len(parts) < 2:
            continue
        name, tok = parts[0].strip(), parts[1].strip()
        if name and len(tok) >= _MIN_TOKEN_LEN:
            out.append((name, tok, _scopes(parts[2] if len(parts) > 2 else "")))
    return out


def _db_has_tokens() -> bool:
    try:
        init_db()
        with system_scope(), SessionLocal() as s:
            return bool(s.execute(select(func.count(AdminToken.id)).where(AdminToken.revoked_at.is_(None))).scalar())
    except Exception:
        return False


def admin_configured() -> bool:
    return bool(_legacy_tokens()) or bool(_env_tokens()) or _db_has_tokens()


def resolve(supplied: str) -> Optional[AdminPrincipal]:
    """Principal for a presented token, or None. Constant-time per candidate."""
    if not supplied:
        return None
    digest = _sha(supplied)
    found: Optional[AdminPrincipal] = None
    for name, tok, scopes in _env_tokens():
        if hmac.compare_digest(_sha(tok), digest):
            found = found or AdminPrincipal(name=name, source="env", scopes=scopes)
    for legacy in _legacy_tokens():
        if hmac.compare_digest(_sha(legacy), digest):
            found = found or AdminPrincipal(name="legacy", source="legacy", scopes=frozenset({"*"}))
    if found:
        return found
    init_db()
    with system_scope(), SessionLocal() as s:
        row = s.execute(
            select(AdminToken).where(AdminToken.token_sha256 == digest, AdminToken.revoked_at.is_(None))
        ).scalar_one_or_none()
        if row is None:
            return None
        row.last_used_at = datetime.now(timezone.utc)
        s.commit()
        return AdminPrincipal(name=row.name, source="db", scopes=_scopes(row.scopes), token_id=row.id)


# ---------------------------------------------------------------- token management


def create_token(name: str, scopes: str = "*") -> str:
    """Create a DB token; returns the plaintext once (only its hash is stored)."""
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@\-]{1,64}", name):
        raise ValueError("name must be 1-64 chars of [A-Za-z0-9_.@-]")
    sc = _scopes(scopes)
    unknown = sc - set(ALL_SCOPES) - {"*"}
    if unknown:
        raise ValueError(f"unknown scopes: {sorted(unknown)}")
    token = "hat_" + secrets.token_urlsafe(32)
    init_db()
    with system_scope(), SessionLocal() as s:
        s.add(AdminToken(name=name, token_sha256=_sha(token), scopes="|".join(sorted(sc))))
        s.commit()
    return token


def list_tokens() -> list[dict[str, Any]]:
    init_db()
    with system_scope(), SessionLocal() as s:
        rows = s.execute(select(AdminToken).order_by(AdminToken.id)).scalars().all()
        return [
            {
                "id": r.id,
                "name": r.name,
                "scopes": r.scopes,
                "fingerprint": r.token_sha256[:12],
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "revoked": r.revoked_at is not None,
            }
            for r in rows
        ]


def revoke_token(token_id: Optional[int] = None, *, name: str = "") -> int:
    init_db()
    with system_scope(), SessionLocal() as s:
        q = select(AdminToken).where(AdminToken.revoked_at.is_(None))
        if token_id is not None:
            q = q.where(AdminToken.id == int(token_id))
        elif name:
            q = q.where(AdminToken.name == name)
        else:
            return 0
        rows = s.execute(q).scalars().all()
        now = datetime.now(timezone.utc)
        for r in rows:
            r.revoked_at = now
        s.commit()
        return len(rows)


# ---------------------------------------------------------------- audit

_ID_PARAMS = {"user_id": "u", "thread_id": "t"}


def _audit_query(request: Request) -> str:
    from app.agent.harness.identity import pseudonym
    from app.agent.harness.redact import redact_text

    parts = []
    for k, v in request.query_params.multi_items():
        if k in _ID_PARAMS and v:
            v = pseudonym(v, _ID_PARAMS[k])
        elif k.lower() in ("token", "key", "secret", "authorization"):
            v = "***"
        parts.append(f"{k}={redact_text(v, max_len=120)}")
    return "&".join(parts)[:2000]


def _audit_path(request: Request) -> str:
    from app.agent.harness.identity import thread_pseudonym

    path = request.url.path
    m = re.fullmatch(r"(/api/traces/)([^/]+)", path)
    if m:
        path = m.group(1) + thread_pseudonym(m.group(2))
    m = re.fullmatch(r"(/api/admin/users/)([^/]+)(.*)", path)
    if m:
        from app.agent.harness.identity import pseudonym
        path = m.group(1) + pseudonym(m.group(2), "u") + m.group(3)
    return path[:256]


def write_audit(
    request: Request,
    *,
    outcome: str,
    status: int,
    scope: str,
    principal: Optional[AdminPrincipal] = None,
    supplied: str = "",
) -> None:
    init_db()
    client = request.client.host if request.client else ""
    with system_scope(), SessionLocal() as s:
        s.add(
            AdminAudit(
                principal=principal.name if principal else "",
                source=principal.source if principal else "",
                outcome=outcome,
                status=status,
                scope=scope,
                method=request.method[:8],
                path=_audit_path(request),
                query=_audit_query(request),
                client=client[:64],
                user_agent=(request.headers.get("user-agent") or "")[:256],
                token_fp=_sha(supplied)[:12] if supplied and not principal else "",
            )
        )
        s.commit()


def _best_effort_audit(request: Request, **kw: Any) -> None:
    try:
        write_audit(request, **kw)
    except Exception as exc:
        log.warning("admin audit write failed (%s)", type(exc).__name__)


def query_audit(*, principal: str = "", outcome: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
    init_db()
    conds = []
    if principal:
        conds.append(AdminAudit.principal == principal)
    if outcome:
        conds.append(AdminAudit.outcome == outcome)
    limit = max(1, min(int(limit or 100), 500))
    with system_scope(), SessionLocal() as s:
        total = s.execute(select(func.count(AdminAudit.id)).where(*conds)).scalar() or 0
        rows = s.execute(
            select(AdminAudit).where(*conds).order_by(AdminAudit.id.desc()).offset(max(0, offset)).limit(limit)
        ).scalars().all()
        by_outcome = dict(
            s.execute(select(AdminAudit.outcome, func.count(AdminAudit.id)).where(*conds).group_by(AdminAudit.outcome)).all()
        )
    return {
        "total": int(total),
        "by_outcome": {k: int(v) for k, v in by_outcome.items()},
        "events": [
            {
                "id": r.id,
                "at": r.at.isoformat() if r.at else None,
                "principal": r.principal,
                "source": r.source,
                "outcome": r.outcome,
                "status": r.status,
                "scope": r.scope,
                "method": r.method,
                "path": r.path,
                "query": r.query,
                "client": r.client,
                "user_agent": r.user_agent,
                "token_fp": r.token_fp,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------- dependency


def _supplied(request: Request) -> str:
    auth = request.headers.get("authorization") or ""
    tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return tok or (request.headers.get("x-admin-token") or "").strip()


def require_scope(scope: str) -> Callable[[Request], AdminPrincipal]:
    def _dep(request: Request) -> AdminPrincipal:
        if getattr(request.state, "admin_verified_scope", None) == scope:
            return request.state.admin
        supplied = _supplied(request)
        if not admin_configured():
            _best_effort_audit(request, outcome="disabled", status=503, scope=scope, supplied=supplied)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "admin_disabled",
                    "message": f"未配置管理员令牌（HARNESS_ADMIN_TOKENS / admin_tokens 表 / HARNESS_ADMIN_TOKEN / ADMIN_TOKEN，至少 {_MIN_TOKEN_LEN} 位），管理接口关闭",
                },
            )
        try:
            principal = resolve(supplied)
        except Exception as exc:
            log.warning("admin token lookup failed (%s)", type(exc).__name__)
            principal = None
        if principal is None:
            _best_effort_audit(request, outcome="denied", status=401, scope=scope, supplied=supplied)
            raise HTTPException(
                status_code=401,
                detail={"code": "admin_unauthorized", "message": "需要管理员令牌"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        if scope and not principal.allows(scope):
            _best_effort_audit(request, outcome="forbidden", status=403, scope=scope, principal=principal)
            raise HTTPException(
                status_code=403,
                detail={"code": "admin_forbidden", "message": f"令牌缺少权限 {scope}"},
            )
        try:
            write_audit(request, outcome="allowed", status=200, scope=scope, principal=principal)
        except Exception as exc:
            log.warning("admin audit write failed (%s); refusing request", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail={"code": "admin_audit_unavailable", "message": "审计日志不可写，管理接口暂停"},
            )
        request.state.admin = principal
        request.state.admin_verified_scope = scope
        return principal

    _dep._admin_scope = scope
    _dep.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return _dep


require_admin = require_scope("traces:read")


class AuditedAdminRoute(APIRoute):
    """Authenticate and audit before JSON parsing/validation, including rejects."""

    def get_route_handler(self):
        handler = super().get_route_handler()
        if not self.path.startswith("/api/admin/"):
            return handler
        scope = next((dep.call._admin_scope for dep in self.dependant.dependencies
                      if hasattr(dep.call, "_admin_scope")), "")
        authorize = require_scope(scope)

        async def audited(request):
            await run_in_threadpool(authorize, request)
            return await handler(request)

        return audited
