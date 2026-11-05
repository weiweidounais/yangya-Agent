"""S1/#5: server-side session identity for every user-scoped /api endpoint.

The caller's user is derived ONLY from a server-signed session token
(``Authorization: Bearer`` / ``X-User-Token`` / HttpOnly ``ja_session`` cookie,
minted by ``POST /api/session``). A client-supplied ``user_id`` (query, JSON
body, path segment, form) is never trusted:

* missing  -> the session user is injected, so handlers keep their signatures;
* matching -> passes through unchanged;
* different -> the same 404 as an unknown owner.

A ``thread_id`` that belongs to another user answers not-found.
Without ``AUTH_SECRET`` the protected surface fails closed (503).
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from app.data_access import user_scope, system_scope

from app.auth import SESSION_COOKIE, auth_configured, verify_user_token
from app.agent.harness.context import step_context


# Paths that never carry a browser user, or carry their own credential
# (admin token, signed link token, channel signature).
EXEMPT_EXACT = {
    "/api/health",
    "/api/capabilities",
    "/api/session",
    "/api/channels",
    "/api/channels/auto-bind",
    "/api/channels/bind",
    "/api/channels/bindings",
    "/api/channels/link",
    "/api/channels/bind/complete",
    "/api/sandbox/submit",
    "/api/sandbox/draft",
    "/api/proactive/tick",
}
EXEMPT_PREFIXES = ("/api/admin/", "/api/traces/")


def is_protected(path: str) -> bool:
    if not path.startswith("/api/"):
        return False
    if path in EXEMPT_EXACT:
        return False
    return not path.startswith(EXEMPT_PREFIXES)


def token_from_scope(headers: dict[str, str], cookies: dict[str, str]) -> str:
    raw = headers.get("authorization") or ""
    tok = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
    return tok or (headers.get("x-user-token") or "").strip() or (cookies.get(SESSION_COOKIE) or "").strip()


def session_user(request: Request) -> Optional[str]:
    tok = token_from_scope(
        {k.lower(): v for k, v in request.headers.items()}, dict(request.cookies)
    )
    return verify_user_token(tok) if tok else None


def new_anonymous_user_id() -> str:
    return "u_" + secrets.token_hex(12)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


def _thread_owner(thread_id: str) -> Optional[str]:
    try:
        from app.graph import get_thread_snapshot

        with system_scope():
            snap = get_thread_snapshot(thread_id)
    except Exception:
        return None
    if not snap.get("exists"):
        return None
    return str(snap.get("user_id") or "").strip() or None



class SessionIdentityMiddleware:
    def __init__(self, app, router=None):
        self.app = app
        self.router = router

    def _path_params(self, scope) -> dict[str, Any]:
        router = self.router
        if router is None:
            return {}
        for route in router.routes:
            try:
                match, child = route.matches(scope)
            except Exception:
                continue
            if match == Match.FULL:
                return dict(child.get("path_params") or {})
        return {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") == "OPTIONS" or not is_protected(scope["path"]):
            return await self.app(scope, receive, send)

        request = Request(scope)
        if not auth_configured():
            return await _err(503, "auth_not_configured", "AUTH_SECRET 未配置，接口已关闭")(scope, receive, send)
        uid = session_user(request)
        if not uid:
            return await _err(401, "unauthenticated", "会话已失效，请刷新页面")(scope, receive, send)

        from fastapi import HTTPException
        from starlette.concurrency import run_in_threadpool
        from app.admin.users import check_enabled

        try:
            await run_in_threadpool(check_enabled, uid)
        except HTTPException as exc:
            return await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(scope, receive, send)

        def denied_thread(thread_id):
            tid = str(thread_id or "").strip()
            if not tid:
                return False
            owner = _thread_owner(tid)
            return owner != uid

        # Path segment user_id / thread_id
        params = self._path_params(scope)
        if "user_id" in params and str(params["user_id"]).strip() != uid:
            return await _err(404, "not_found", "没有找到这条记录")(scope, receive, send)
        if "thread_id" in params and denied_thread(params["thread_id"]):
            return await _err(404, "thread_not_found", "thread_id 不存在或无可确认的会话")(scope, receive, send)

        # Query string
        q = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        claimed = [v for k, v in q if k == "user_id" and v.strip()]
        if any(v.strip() != uid for v in claimed):
            return await _err(404, "not_found", "没有找到这条记录")(scope, receive, send)
        if any(k == "thread_id" and denied_thread(v) for k, v in q):
            return await _err(404, "thread_not_found", "thread_id 不存在或无可确认的会话")(scope, receive, send)
        q = [(k, v) for k, v in q if k != "user_id"] + [("user_id", uid)]
        scope = dict(scope)
        scope["query_string"] = urlencode(q).encode("latin-1")
        scope.setdefault("state", {})
        scope["state"] = dict(scope["state"], session_user=uid)
        thread_id = params.get("thread_id") or dict(q).get("thread_id") or ""

        ctype = (dict((k.decode().lower(), v.decode()) for k, v in scope.get("headers", [])).get("content-type") or "")
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE") and "application/json" in ctype:
            chunks = []
            more = True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            raw = b"".join(chunks)
            try:
                body = json.loads(raw.decode("utf-8") or "null")
            except (ValueError, UnicodeDecodeError):
                body = None
            if isinstance(body, dict):
                c = body.get("user_id")
                if c not in (None, "") and str(c).strip() != uid:
                    return await _err(404, "not_found", "没有找到这条记录")(scope, receive, send)
                if denied_thread(body.get("thread_id")):
                    return await _err(404, "thread_not_found", "thread_id 不存在或无可确认的会话")(scope, receive, send)
                thread_id = body.get("thread_id") or thread_id
                body["user_id"] = uid
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                scope["headers"] = [
                    (k, v) for k, v in scope["headers"] if k.lower() != b"content-length"
                ] + [(b"content-length", str(len(raw)).encode())]
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": raw, "more_body": False}
                return await receive()

            # Pre-graph tools (URL gating and routing) need the same trusted
            # identity as graph nodes. Context also propagates to sync workers.
            with user_scope(uid), step_context(user_id=uid, thread_id=thread_id):
                from app.agent.schedules.activity import heartbeat
                await run_in_threadpool(heartbeat, uid)
                return await self.app(scope, replay, send)
        with user_scope(uid), step_context(user_id=uid, thread_id=thread_id):
            from app.agent.schedules.activity import heartbeat
            await run_in_threadpool(heartbeat, uid)
            return await self.app(scope, receive, send)
