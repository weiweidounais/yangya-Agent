"""FastAPI entry — Personal Agent."""

from __future__ import annotations

from app.data_access import system_scope, user_scope

import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import GIT_SHA, VERSION_TOKEN, __version__
from app import confirm_store, db, turn_results
from app.auth import require_user
from app.agent.harness.admin import AdminPrincipal, AuditedAdminRoute, require_admin, require_scope
from app.data_access.public import PublicWriteForbidden
from app.observability.middleware import TraceMiddleware
from app.agent.harness.leak import public_profile
from app.channels import (
    apply_auto_bindings,
    bind_channel,
    list_bindings,
    parse_feishu_webhook,
    parse_openclaw_webhook,
    unbind_channel,
)
from app.gateway.llm_gateway import gateway_info, has_llm
from app.graph import ContinueError, continue_task, get_thread_snapshot, new_thread_id, run_action, update_thread_state
from app.llm import capabilities
from app.models import (
    ApiResponse,
    BindChannelRequest,
    ChatRequest,
    CompanionMealRequest,
    CompanionProfileRequest,
    CompanionReportRequest,
    CompanionReviveRequest,
    ConfirmRequest,
    ContinueTaskRequest,
    DraftRequest,
    RestTimerRequest,
    UpsertProfileRequest,
)
from app.secrets_vault import is_configured as secrets_vault_configured
from app.agent.tool_registry import ToolStatusUnknown

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

load_dotenv(ROOT / ".env")

app = FastAPI(title="Personal Agent", version=__version__)
app.router.route_class = AuditedAdminRoute

# Browser chat from other origins (same-origin on Render also fine).
# CORS_ALLOW_ORIGINS=* (default) or comma-separated list.
_cors_raw = (os.getenv("CORS_ALLOW_ORIGINS") or "*").strip()
_cors_origins = (
    ["*"]
    if _cors_raw == "*"
    else [o.strip() for o in _cors_raw.split(",") if o.strip()]
)
app.add_middleware(TraceMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-Id"],
)
from app.admin.api import router as admin_router  # noqa: E402

app.include_router(admin_router)
# Every user-scoped /api call is bound to the server session (outermost).
from app.session_identity import SESSION_COOKIE, SessionIdentityMiddleware, new_anonymous_user_id, session_user  # noqa: E402

app.add_middleware(SessionIdentityMiddleware, router=app.router)


@app.post("/api/session")
def create_session(request: Request):
    """Issue (or refresh) the browser session. The user id is chosen by the
    server, never by the client; an existing valid session is kept."""
    from fastapi.responses import JSONResponse

    from app.auth import auth_configured, mint_user_token

    if not auth_configured():
        raise HTTPException(503, detail={"code": "auth_not_configured", "message": "AUTH_SECRET 未配置，接口已关闭"})
    uid = session_user(request) or new_anonymous_user_id()
    resp = JSONResponse({"ok": True, "status": "session", "data": {"user_id": uid}})
    resp.set_cookie(
        SESSION_COOKIE,
        mint_user_token(uid),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=30 * 24 * 3600,
        path="/",
    )
    return resp


def _require_user(user_id: Optional[str]) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(
            status_code=400,
            detail={"message": "user_id required", "code": "user_required"},
        )
    return uid


def _app_tz() -> str:
    return (os.getenv("PROACTIVE_TZ") or os.getenv("APP_TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai"


class MemoryFactBody(BaseModel):
    value: str = ""
    thread_id: Optional[str] = None


class MemoryEventBody(BaseModel):
    value: str = ""
    thread_id: Optional[str] = None


class MemoryForgetBody(BaseModel):
    confirm: bool = False
    thread_id: Optional[str] = None


class ConfirmThreadError(Exception):
    """Confirm against missing / foreign thread_id."""

    def __init__(self, message: str, *, http_status: int = 404, code: str = "thread_not_found"):
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code


def require_confirmable_thread(thread_id: str, user_id: str) -> dict[str, Any]:
    """Ensure thread checkpoint exists and belongs to user_id."""
    tid = str(thread_id or "").strip()
    uid = str(user_id or "").strip()
    if not tid:
        raise ConfirmThreadError("thread_id required", http_status=400, code="thread_required")
    if not uid:
        raise ConfirmThreadError("user_id required", http_status=400, code="user_required")
    snap = get_thread_snapshot(tid)
    if not snap.get("exists"):
        raise ConfirmThreadError(
            "thread_id 不存在或无可确认的会话",
            http_status=404,
            code="thread_not_found",
        )
    owner = str(snap.get("user_id") or "").strip()
    if owner != uid:
        raise ConfirmThreadError(
            "thread_id 不存在或无可确认的会话",
            http_status=404,
            code="thread_not_found",
        )
    return snap


def _with_confirm_flags(data: dict[str, Any], awaiting: bool) -> dict[str, Any]:
    """Task-card shape: needs_confirm + draft payload."""
    out = dict(data or {})
    out["needs_confirm"] = bool(awaiting)
    if awaiting:
        draft = out.get("draft") or {}
        interrupt = out.get("interrupt") or {}
        out["confirm_actions"] = [
            {"id": "confirm", "label": "确认定稿", "confirmed": True},
            {"id": "reject", "label": "拒绝", "confirmed": False},
        ]
        out["task_card"] = {
            "type": "draft_confirm",
            "title": "待确认：草稿",
            "kind": draft.get("kind") or interrupt.get("kind") or "other",
            "draft_title": draft.get("title") or interrupt.get("draft_title") or "",
            "body": draft.get("body") or interrupt.get("body") or "",
            "policy": "no_expiry_later_overrides",
        }
    return out


def _apply_confirm(**kwargs) -> dict[str, Any]:
    from app.business_stage import stage_business_writes
    from app.agent.node_wrap import NodeWriteFailure, NODE_FAILED, _fail_closed

    try:
        with stage_business_writes():
            return _apply_confirm_transaction(**kwargs)
    except ConfirmThreadError:
        raise
    except Exception as exc:
        result = exc.result if isinstance(exc, NodeWriteFailure) else _fail_closed(NODE_FAILED)
        from app.graph import failure_exit

        result = failure_exit(kwargs["thread_id"], result)["state"]
        return {
            "thread_id": kwargs["thread_id"], "status": "error",
            "awaiting_confirm": False, "decision": None,
            "data": {**result, "interrupt": None, "needs_confirm": False, "decision": None},
        }


def _apply_confirm_transaction(
    *,
    user_id: str,
    thread_id: str,
    confirmed: bool,
    edits: Optional[dict[str, Any]] = None,
    channel: str = "web",
    reason: Optional[str] = None,
    confirm_token: str = "",
) -> dict[str, Any]:
    """Record decision (later overrides) and resume the graph if still interrupted.

    Reject / later override to reject: mark the checkpoint unconfirmed.
    Confirm cards never expire; later click always wins in confirm_store.
    Nonexistent / foreign thread_id raises ConfirmThreadError — no decision row.
    """
    snap = require_confirmable_thread(thread_id, user_id)
    from app.agent.node_wrap import NodeWriteFailure, NODE_FAILED, GUARD_UNAVAILABLE, _fail_closed

    prior_error = str((snap.get("values") or {}).get("error") or "")
    if not snap.get("next") and prior_error.startswith((NODE_FAILED, GUARD_UNAVAILABLE)):
        raise NodeWriteFailure(_fail_closed(GUARD_UNAVAILABLE if prior_error.startswith(GUARD_UNAVAILABLE) else NODE_FAILED))

    decision = confirm_store.record_decision(
        thread_id,
        user_id,
        confirmed,
        channel=channel,
        edits=edits,
    )
    graph_out: dict[str, Any] = {
        "thread_id": thread_id,
        "state": {},
        "awaiting_confirm": False,
        "interrupt": None,
        "next": [],
    }
    resumed = False
    if snap.get("next"):
        try:
            graph_out = run_action(
                {
                    "user_id": user_id,
                    "_resume": {
                        "confirmed": confirmed,
                        "confirm_token": confirm_token,
                        "edits": edits or {},
                        "reason": (reason or "").strip()[:500] if not confirmed else "",
                    },
                },
                thread_id=thread_id,
            )
            resumed = True
        except Exception:
            raise NodeWriteFailure(_fail_closed(NODE_FAILED))
    else:
        graph_out["state"] = {"confirmed": confirmed, "messages": [], "draft": (snap.get("values") or {}).get("draft")}

    state = dict(graph_out.get("state") or {})
    if str(state.get("error") or "").startswith((NODE_FAILED, GUARD_UNAVAILABLE)):
        raise NodeWriteFailure(state)
    awaiting_again = bool(graph_out.get("awaiting_confirm"))
    pending_redraft = bool(state.get("pending_redraft")) or (
        (not confirmed) and awaiting_again
    )

    if confirmed:
        status = "confirmed"
    elif pending_redraft or awaiting_again:
        # Loop: auto re-draft → ask user again (HTTP stays compatible)
        status = "awaiting_confirm"
    else:
        status = "rejected"

    side_effect_note = None

    if not confirmed and not (pending_redraft or awaiting_again):
        # Final reject (including later override): the checkpoint must not stay confirmed.
        if state.get("confirmed") is not False:
            saved = update_thread_state(thread_id, {"confirmed": False})
            if saved["state"].get("error"):
                raise NodeWriteFailure(saved["state"])
        state["confirmed"] = False
        if decision.get("overridden"):
            side_effect_note = "确认结果已覆盖为拒绝（不过期/后点生效）。"
        else:
            side_effect_note = "已拒绝草稿。"
    elif not confirmed and (pending_redraft or awaiting_again):
        side_effect_note = (
            f"已拒绝并自动重出稿（loop_redraft="
            f"{(state.get('loop_redraft_count') or 0)}）；请再次确认。"
        )
    elif decision.get("overridden") and not resumed:
        side_effect_note = "确认结果已覆盖写入（不过期/后点生效）；图已越过中断，以确认结果为准。"

    if not resumed:
        try:
            from app.agent.memory import write_event

            write_event(
                user_id,
                "confirm_accepted" if confirmed else "confirm_rejected",
                side_effect_note or ("确认" if confirmed else "拒绝"),
                tags=["confirm", "override" if decision.get("overridden") else "decision"],
                payload={
                    "confirmed": confirmed,
                    "overridden": bool(decision.get("overridden")),
                    "channel": channel,
                },
                thread_id=thread_id,
            )
        except Exception:
            pass

    awaiting_flag = bool(graph_out.get("awaiting_confirm")) or bool(pending_redraft and awaiting_again)
    return {
        "thread_id": thread_id,
        "status": status,
        "awaiting_confirm": awaiting_flag,
        "decision": decision,
        "resumed_graph": resumed,
        "data": {
            "confirmed": confirmed,  # latest decision wins for API consumers
            "graph_confirmed": state.get("confirmed"),
            "draft": state.get("draft"),
            "messages": state.get("messages") or [],
            "needs_confirm": awaiting_flag,
            "decision": decision,
            "override_allowed": True,
            "side_effect_note": side_effect_note,
            "loop": {
                "step": state.get("loop_step"),
                "redraft_count": state.get("loop_redraft_count"),
                "last_error": state.get("loop_last_error"),
                "pending_redraft": pending_redraft,
                "tokens": state.get("loop_tokens"),
                "stop_reason": state.get("loop_stop_reason"),
                "feedback": state.get("loop_feedback") or [],
            },
            "reject_reason": (reason or "").strip()[:500] if not confirmed else None,
            "next_suggestion": state.get("next_suggestion"),
            "interrupt": graph_out.get("interrupt"),
        },
    }


@app.on_event("startup")
def _startup() -> None:
    from app.agent.intent_scoring import warmup
    warmup()
    db.init_db()
    # Ensure confirm / binding / memory tables exist
    from app.channels.binding import ChannelBinding  # noqa: F401
    from app.channels.conversation import ChannelConversation  # noqa: F401
    from app.channels.links import ChannelLinkToken  # noqa: F401
    from app.confirm_store import ConfirmDecision  # noqa: F401
    from app.agent.memory.episodic import EpisodicEvent  # noqa: F401
    from app.agent.harness.traces import TraceStep  # noqa: F401
    from app.agent.proactive.store import ProactiveItem, ProactiveRun  # noqa: F401
    from app.secrets_vault import UserSecret  # noqa: F401

    db.init_db()
    # Env-driven Feishu/OpenClaw ↔ user binding
    try:
        import logging

        applied = apply_auto_bindings()
        logging.getLogger("uvicorn.error").info(
            "channel auto-bind: %s binding(s)", len(applied)
        )
    except Exception as exc:
        import logging

        logging.getLogger("uvicorn.error").warning("channel auto-bind failed: %s", exc)
    from app.graph import get_graph

    get_graph()
    # Feishu long connection (no public webhook URL); only when FEISHU_ENABLED=1
    try:
        from app.channels import feishu_ws
        import logging

        info = feishu_ws.start_background()
        logging.getLogger("uvicorn.error").info("feishu_ws start: %s", info)
    except Exception as exc:
        import logging

        logging.getLogger("uvicorn.error").warning("feishu_ws not started: %s", exc)

    # Proactive scheduler (default OFF via PROACTIVE_ENABLED)
    try:
        from app.agent import proactive as proactive_mod
        import logging

        info = proactive_mod.start()
        logging.getLogger("uvicorn.error").info("proactive start: %s", info)
    except Exception as exc:
        import logging

        logging.getLogger("uvicorn.error").warning("proactive not started: %s", exc)


@app.on_event("shutdown")
def _shutdown() -> None:
    try:
        from app.agent import proactive as proactive_mod

        proactive_mod.stop()
    except Exception:
        pass
    try:
        from app.channels import feishu_ws

        feishu_ws.stop_background()
    except Exception:
        pass


@app.get("/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    info = gateway_info()
    routing = info["routing"]
    from app.channels import feishu_client, feishu_ws, openclaw_wechat
    from app.channels.registry import channels_status

    proactive_status: dict[str, Any] = {}
    try:
        from app.agent import proactive as proactive_mod

        proactive_status = proactive_mod.status()
    except Exception as exc:
        proactive_status = {"proactive_enabled": False, "error": str(exc)[:200]}

    return {
        "ok": True,
        "service": "personal-agent",
        "llm": "live" if has_llm() else "mock",
        "fast": routing["fast"],
        "strong": routing["strong"],
        "routing": routing,
        "gateway": info,
        "feishu": feishu_client.config_status(),
        "feishu_ws": feishu_ws.status(),
        "openclaw": openclaw_wechat.config_status(),
        "channels": channels_status(),
        "version": __version__,
        "git_sha": GIT_SHA,
        "tz": _app_tz(),
        "proactive_enabled": bool(proactive_status.get("proactive_enabled")),
        "proactive": {
            "enabled": bool(proactive_status.get("proactive_enabled")),
            "scheduler_alive": bool(proactive_status.get("scheduler_alive")),
            "next_run_hint": proactive_status.get("next_run_hint"),
        },
        "secrets_vault": {"configured": secrets_vault_configured()},
    }


@app.get("/api/capabilities")
def api_capabilities() -> dict[str, Any]:
    caps = capabilities()
    caps["gateway"] = gateway_info()
    from app.channels import feishu_client, openclaw_wechat
    from app.channels.registry import list_adapters

    caps["channels"] = ["web"] + [a.name for a in list_adapters() if a.enabled()]
    caps["channels_reserved"] = [a.name for a in list_adapters() if a.reserved]

    caps["feishu"] = feishu_client.config_status()
    caps["openclaw"] = openclaw_wechat.config_status()
    return caps


@app.get("/api/profile/{user_id}")
def get_profile(user_id: str) -> ApiResponse:
    return ApiResponse(data={"profile": public_profile(db.get_profile(user_id), user_id=user_id)})


@app.post("/api/profile")
def upsert_profile(body: UpsertProfileRequest) -> ApiResponse:
    payload: dict[str, Any] = {"user_id": body.user_id, "action": "update_profile"}
    if body.profile:
        payload["profile"] = body.profile.model_dump()
    out = run_action(payload, thread_id=new_thread_id())
    state = out["state"]
    return ApiResponse(
        thread_id=out["thread_id"],
        status="profile_updated",
        data={
            "profile": public_profile(
                state.get("profile") or db.get_profile(body.user_id), user_id=body.user_id
            ),
            "messages": state.get("messages"),
        },
    )


def _draft_response(out: dict[str, Any], user_id: str, extra_messages: Optional[list] = None) -> ApiResponse:
    state = out["state"]
    merged_msgs = list(extra_messages or []) + list(state.get("messages") or [])
    data = _with_confirm_flags(
        {
            "intent": "draft",
            "error": state.get("error"),
            "draft": state.get("draft"),
            "interrupt": out.get("interrupt"),
            "messages": merged_msgs,
            "profile": public_profile(state.get("profile"), user_id=user_id),
            "loop": out.get("loop"),
            "continue_card": state.get("pending_continue"),
        },
        out["awaiting_confirm"],
    )
    return ApiResponse(
        thread_id=out["thread_id"],
        status="awaiting_confirm" if out["awaiting_confirm"] else "error" if state.get("error") else "drafted",
        awaiting_confirm=out["awaiting_confirm"],
        data=data,
    )


@app.post("/api/draft")
def draft(body: DraftRequest) -> ApiResponse:
    return turn_results.run(body.request_id, lambda: _draft(body))


def _draft(body: DraftRequest) -> ApiResponse:
    request = (body.request or "").strip()
    if not request:
        raise HTTPException(400, "request required")
    redacted, hits = _privacy_gate(request)
    tid = body.thread_id or new_thread_id()
    if hits:
        return _privacy_response(body.user_id, tid, redacted, hits, raw=request)
    _require_thread_owner(body.thread_id, body.user_id)
    out = run_action(
        {
            "user_id": body.user_id,
            "action": "draft",
            "message": request,
            "draft_request": request,
            "channel": "web",
        },
        thread_id=tid,
    )
    return _draft_response(out, body.user_id)


@app.exception_handler(ToolStatusUnknown)
async def tool_outcome_unknown(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=202, content=exc.as_dict())


@app.exception_handler(PublicWriteForbidden)
async def public_write_forbidden(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.http_status,
                        content={"detail": {"code": exc.code, "message": str(exc)}})


@app.post("/api/confirm/{thread_id}/token")
def confirm_token(thread_id: str, user_id: str = Query("")) -> ApiResponse:
    """Mint a one-time confirm token for the session user's own thread."""
    uid = _require_user(user_id)
    try:
        snapshot = require_confirmable_thread(thread_id, uid)
    except ConfirmThreadError as exc:
        raise HTTPException(exc.http_status, detail={"code": exc.code, "message": exc.message}) from exc
    return ApiResponse(
        thread_id=thread_id,
        status="confirm_token",
        data={"confirm_token": confirm_store.issue_card_token(thread_id, uid, snapshot.get("values") or {})},
    )


@app.post("/api/confirm")
def confirm(body: ConfirmRequest) -> ApiResponse:
    try:
        require_confirmable_thread(body.thread_id, body.user_id)
        confirm_store.consume_token(body.confirm_token or "", body.thread_id, body.user_id)
    except ConfirmThreadError as exc:
        raise HTTPException(exc.http_status, detail={"code": exc.code, "message": exc.message}) from exc
    except confirm_store.ConfirmTokenError as exc:
        raise HTTPException(exc.http_status, detail={"code": exc.code, "message": exc.message}) from exc
    try:
        result = _apply_confirm(
            user_id=body.user_id,
            thread_id=body.thread_id,
            confirmed=body.confirmed,
            edits=body.edits,
            channel=body.channel or "web",
            reason=body.reason,
            confirm_token=body.confirm_token or "",
        )
    except ConfirmThreadError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"message": exc.message, "code": exc.code, "thread_id": body.thread_id},
        ) from exc
    return ApiResponse(
        thread_id=result["thread_id"],
        status=result["status"],
        awaiting_confirm=result["awaiting_confirm"],
        data=_with_confirm_flags(result["data"], result["awaiting_confirm"]),
    )


@app.get("/api/confirm/{thread_id}")
def get_confirm(thread_id: str, user_id: str = Query("")) -> ApiResponse:
    d = confirm_store.get_decision(thread_id)
    if d and str(d.get("user_id") or "") != _require_user(user_id):
        raise HTTPException(404, detail={"code": "not_found", "message": "没有找到这条确认"})
    return ApiResponse(
        thread_id=thread_id,
        status="found" if d else "empty",
        data={"decision": d},
    )


def _privacy_gate(text: str) -> tuple[str, list[str]]:
    """Credentials, phone numbers and ID numbers are intercepted before the graph."""
    from app.privacy import redact_sensitive

    return redact_sensitive(text)


def _privacy_response(user_id: str, thread_id: str, redacted: str, hits: list[str], raw: str = "") -> ApiResponse:
    from app.agent.ask_cards import secret_card
    from app.agent.memory import write_event
    from app.privacy import privacy_reply, suggest_secret_key

    key = suggest_secret_key(redacted, hits)
    try:
        # Gate runs before the graph: still leave a (masked) trace so the turn is auditable.
        from app.agent.harness.traces import record_step

        record_step(
            thread_id=thread_id,
            user_id=user_id,
            intent="privacy_gate",
            tool="privacy_gate",
            input_summary=f"[redacted: {','.join(sorted(set(hits)))}] ({len(raw or redacted)}字)",
            result_summary="privacy_redirect",
            detail={"kinds": list(hits)},
        )
    except Exception:
        pass
    try:
        write_event(
            user_id,
            "privacy_redacted",
            "聊天里出现了敏感信息，已隐藏并引导到安全输入区",
            tags=["privacy"],
            payload={"kinds": hits, "key": key},
            thread_id=thread_id,
        )
    except Exception:
        pass
    return ApiResponse(
        thread_id=thread_id,
        status="chat",
        data={
            "intent": "chat",
            "messages": [{"role": "assistant", "content": privacy_reply(key)}],
            "ask": secret_card(key, purpose="你刚才想发的信息，放在这里更安全"),
            "redacted_message": redacted,
            "redact_user_message": True,
            "privacy": {"redacted": hits},
        },
    )


def _require_thread_owner(thread_id: Optional[str], user_id: str) -> None:
    """403 when a caller-supplied thread belongs to someone else."""
    tid = (thread_id or "").strip()
    if not tid:
        return
    snap = get_thread_snapshot(tid)
    owner = str(snap.get("user_id") or "").strip()
    if snap.get("exists") and owner != user_id:
        raise HTTPException(
            status_code=403,
            detail={"code": "thread_forbidden", "message": "thread_id 不属于当前用户"},
        )


def _clean_reply_to(raw: Any) -> Optional[dict[str, Any]]:
    from app.privacy import redact_sensitive

    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for field, limit in (("ask_id", 40), ("type", 16), ("question", 200), ("prompt", 600)):
        val = raw.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = redact_sensitive(val.strip()[:limit])[0]
    return out if out.get("question") else None


def _chat_intent_response(out: dict[str, Any], intent: str, user_id: str = "") -> ApiResponse:
    state = out["state"]
    data: dict[str, Any] = {
        "intent": intent,
        "messages": state.get("messages"),
        "profile": public_profile(state.get("profile"), user_id=user_id),
        "next_suggestion": state.get("next_suggestion"),
        "loop": out.get("loop"),
    }
    data["continue_card"] = state.get("pending_continue")
    if intent == "chat" and state.get("ask"):
        data["ask"] = state.get("ask")
    if state.get("error"):
        data["error"] = state.get("error")
    return ApiResponse(thread_id=out["thread_id"], status="chat", data=data)


# ----- Secret sandbox (encrypted; values never leave the vault) -----


def _secret_owner(user_id: Any) -> str:
    uid = str(user_id or "").strip()
    allow_demo = (os.getenv("SECRETS_ALLOW_DEMO") or "").strip().lower() in ("1", "true", "yes")
    if not uid or (uid == "demo" and not allow_demo):
        raise HTTPException(
            status_code=401,
            detail={"message": "会话已失效，请刷新页面后再保存", "code": "login_required"},
        )
    return uid


def _vault_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"message": "安全保存暂未开启", "code": "vault_unavailable"},
    )


@app.get("/api/secrets")
def secrets_list(user_id: str = Query("")) -> ApiResponse:
    from app import secrets_vault

    uid = _secret_owner(user_id)
    return ApiResponse(
        status="secrets",
        data={"items": secrets_vault.list_secrets(uid), "configured": secrets_vault.is_configured()},
    )


@app.post("/api/secrets")
async def secrets_save(request: Request) -> ApiResponse:
    """Body: {user_id, key, purpose, values:{field: value}}. Parsed by hand so no
    validation error ever echoes submitted values back."""
    import json as _json

    from app import secrets_vault
    from app.agent.memory import write_event

    try:
        body = _json.loads((await request.body()).decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        raise HTTPException(400, detail={"message": "请求格式不对", "code": "bad_request"})
    uid = _secret_owner(body.get("user_id"))
    key = str(body.get("key") or "").strip()
    if key not in secrets_vault.CATALOG:
        raise HTTPException(400, detail={"message": "不支持保存这类信息", "code": "unknown_key"})
    if not secrets_vault.is_configured():
        raise _vault_unavailable()
    try:
        item = secrets_vault.save_secret(
            uid, key, body.get("values"), purpose=str(body.get("purpose") or "")
        )
    except secrets_vault.SecretValidationError as exc:
        raise HTTPException(400, detail={"message": str(exc), "code": "invalid_value"}) from None
    except secrets_vault.VaultUnavailable:
        raise _vault_unavailable() from None
    message = f"已安全保存 {item['label']}"
    try:
        write_event(uid, "secret_saved", message, tags=["secret"], payload={"key": key})
    except Exception:
        pass
    return ApiResponse(status="saved", data={"item": item, "message": message})


@app.delete("/api/secrets/{key}")
def secrets_delete(key: str, user_id: str = Query("")) -> ApiResponse:
    from app import secrets_vault
    from app.agent.memory import write_event

    uid = _secret_owner(user_id)
    if not secrets_vault.delete_secret(uid, key):
        raise HTTPException(404, detail={"message": "没有找到这条保存的信息", "code": "secret_not_found"})
    label = secrets_vault.CATALOG.get(key, {}).get("label", key)
    try:
        write_event(uid, "secret_deleted", f"已删除 {label}", tags=["secret"], payload={"key": key})
    except Exception:
        pass
    return ApiResponse(status="deleted", data={"key": key})


@app.post("/api/continue")
def continue_budget(body: ContinueTaskRequest, uid: str = Depends(require_user)) -> ApiResponse:
    """Continue a task stopped at its budget: +50% of the tier, bounded by cost caps."""
    if body.user_id is not None and body.user_id.strip() != uid:
        raise HTTPException(
            status_code=403,
            detail={"code": "user_mismatch", "message": "user_id 与登录用户不一致"},
        )
    try:
        out = continue_task(body.thread_id, uid)
    except ContinueError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code, "message": exc.message, "thread_id": body.thread_id},
        ) from exc
    state = out["state"]
    return ApiResponse(
        thread_id=out["thread_id"],
        status=out.get("status") or "continued",
        awaiting_confirm=out["awaiting_confirm"],
        data=_with_confirm_flags(
            {
                "draft": state.get("draft"),
                "interrupt": out.get("interrupt"),
                "messages": state.get("messages"),
                "error": state.get("error"),
                "loop": out.get("loop"),
                "continue_card": state.get("pending_continue"),
            },
            out["awaiting_confirm"],
        ),
    )


@app.post("/api/chat")
def chat(body: ChatRequest) -> ApiResponse:
    return turn_results.run(body.request_id, lambda: _chat(body))


@app.get("/api/turns/{request_id}")
def get_turn_result(request_id: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(turn_results.get(request_id), headers={"Cache-Control": "no-store"})


def _chat(body: ChatRequest) -> ApiResponse:
    """
    Chat driver:
    - privacy gate first (credentials / phone / ID never reach the graph)
    - typed reject on a draft that is waiting for the card
    - model-selected schedule tools (reminders) with clarification turns
    - writing requests → draft → confirm card
    - otherwise plain chat with memory
    """
    from app.agent.router import parse_reject_command

    tid = body.thread_id or new_thread_id()
    _require_thread_owner(body.thread_id, body.user_id)
    msg = (body.message or "").strip()

    if msg:
        redacted, hits = _privacy_gate(msg)
        if hits:
            return _privacy_response(body.user_id, tid, redacted, hits, raw=msg)
        from app.companion.store import observe_message

        observe_message(body.user_id, msg)

    from app.agent.router import intent_result_for
    intent_state = {"user_id": body.user_id, "message": msg, "action": ""}
    intent_result_for(intent_state, msg)
    reply_to = _clean_reply_to(body.reply_to)
    if reply_to and msg:
        from app.agent.ask_cards import selected_intent
        selected = selected_intent(msg, reply_to)
        original = msg
        if selected:
            previous = get_thread_snapshot(tid).get("values") or {}
            card = previous.get("ask") or {}
            if card.get("id") != reply_to.get("ask_id") or msg not in card.get("options", []):
                selected = None
            elif selected != "chat":
                original = previous.get("message") or msg
        intent = selected or "chat"
        if intent == "draft":
            out = run_action(
                {**intent_state, "action": "draft", "message": original, "draft_request": original, "channel": "web"},
                thread_id=new_thread_id(),
            )
            return _draft_response(out, body.user_id)
        out = run_action(
            {**intent_state, "action": intent, "message": original, "reply_to": reply_to, "channel": "web"},
            thread_id=tid,
        )
        return _chat_intent_response(out, intent, user_id=body.user_id)

    if not msg:
        return ApiResponse(
            thread_id=tid,
            status="error",
            data={
                "messages": [{"role": "assistant", "content": "请先输入内容。"}],
                "error": "empty_message",
            },
        )

    # Typed reject (with optional reason) on a draft that is waiting for the card.
    reject_reason = parse_reject_command(msg)
    if body.thread_id and reject_reason is not None:
        snap = get_thread_snapshot(body.thread_id)
        if snap.get("exists") and "await_confirm" in (snap.get("next") or []):
            try:
                result = _apply_confirm(
                    user_id=body.user_id,
                    thread_id=body.thread_id,
                    confirmed=False,
                    channel="web",
                    reason=reject_reason,
                )
            except ConfirmThreadError as exc:
                raise HTTPException(
                    status_code=exc.http_status,
                    detail={"message": exc.message, "code": exc.code, "thread_id": body.thread_id},
                ) from exc
            return ApiResponse(
                thread_id=result["thread_id"],
                status=result["status"],
                awaiting_confirm=result["awaiting_confirm"],
                data=_with_confirm_flags(result["data"], result["awaiting_confirm"]),
            )

    selected = intent_state["intent_result"]["rule_id"].removeprefix("model_tool:")
    previous = (get_thread_snapshot(tid).get("values") or {}) if body.thread_id else {}
    pending_schedule = previous.get("pending_schedule")
    continuing_schedule = pending_schedule and intent_state["intent_result"]["rule_id"] in (
        "model_tool:none", "chat_fallback",
    )
    if selected.startswith("schedule.") or continuing_schedule:
        from app.agent.schedules.chat import respond
        if continuing_schedule:
            selected = pending_schedule["tool"]
        # Schedule responses bypass run_action, so establish the owned checkpoint
        # before returning a thread ID or invoking a schedule tool.
        saved = update_thread_state(tid, {"user_id": body.user_id, "thread_id": tid})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        data = respond(body.user_id, selected, msg,
                       pending=pending_schedule if pending_schedule and pending_schedule["tool"] == selected else None)
        saved = update_thread_state(tid, {"pending_schedule": data.pop("_pending_schedule", None) if data else None})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        if data is not None:
            return ApiResponse(thread_id=tid, status="schedules", data=data)
    elif pending_schedule:
        update_thread_state(tid, {"pending_schedule": None})

    # Sports-buddy tools: the model picks the tool, the adapter extracts args.
    pending_sport = previous.get("pending_sport")
    continuing_sport = pending_sport and intent_state["intent_result"]["rule_id"] in (
        "model_tool:none", "chat_fallback",
    )
    if selected.startswith("sport.") or continuing_sport:
        from app.sports.chat import respond as sport_respond

        if continuing_sport:
            selected = pending_sport["tool"]
        saved = update_thread_state(tid, {"user_id": body.user_id, "thread_id": tid})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        data = sport_respond(body.user_id, selected, msg, pending=pending_sport)
        saved = update_thread_state(tid, {"pending_sport": data.pop("_pending_sport", None) if data else None})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        if data is not None:
            return ApiResponse(thread_id=tid, status="chat", data={"intent": "chat", **data})
    elif pending_sport:
        update_thread_state(tid, {"pending_sport": None})

    # Companion tools: body setup, meals, status, recap, revive.
    pending_companion = previous.get("pending_companion")
    continuing_companion = pending_companion and intent_state["intent_result"]["rule_id"] in (
        "model_tool:none", "chat_fallback",
    )
    if selected.startswith("companion.") or continuing_companion:
        from app.companion.chat import respond as companion_respond

        if continuing_companion:
            selected = pending_companion["tool"]
        saved = update_thread_state(tid, {"user_id": body.user_id, "thread_id": tid})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        data = companion_respond(body.user_id, selected, msg, pending=pending_companion)
        saved = update_thread_state(tid, {"pending_companion": data.pop("_pending_companion", None) if data else None})
        if saved["state"].get("error"):
            return _chat_intent_response(saved, "chat", user_id=body.user_id)
        if data is not None:
            return ApiResponse(thread_id=tid, status="chat", data={"intent": "chat", **data})
    elif pending_companion:
        update_thread_state(tid, {"pending_companion": None})

    from app.agent.router import route_intent

    intent = route_intent(intent_state, text=msg)
    if intent == "draft":
        # A new writing request starts its own thread so an earlier draft card stays intact.
        draft_tid = tid
        if body.thread_id and previous.get("draft") and previous.get("draft_request") != msg:
            draft_tid = new_thread_id()
        out = run_action(
            {
                "user_id": body.user_id,
                "action": "draft",
                "message": msg,
                "draft_request": msg,
                "intent_result": intent_state["intent_result"],
                "intent_message": intent_state["intent_message"],
                "channel": "web",
            },
            thread_id=draft_tid,
        )
        return _draft_response(out, body.user_id)

    out = run_action(
        {
            "user_id": body.user_id,
            "action": "chat",
            "message": msg,
            "intent_result": intent_state["intent_result"],
            "intent_message": intent_state["intent_message"],
            "channel": "web",
            "reply_to": None,
        },
        thread_id=tid,
    )
    return _chat_intent_response(out, "chat", user_id=body.user_id)


# ----- Memory (episodic, no vectors) -----


def _memory_safe_profile(profile: Any) -> Any:
    """/api/memory never returns long bodies: length + a short PII-masked preview only."""
    from app.agent.harness.redact import redact_text

    if isinstance(profile, list):
        return [_memory_safe_profile(x) for x in profile]
    if not isinstance(profile, dict):
        return profile
    out: dict[str, Any] = {}
    for key, val in profile.items():
        if isinstance(val, str) and val and len(val) > 400:
            out[key] = {"chars": len(val), "preview": redact_text(val.replace("\n", " "), max_len=40), "full_text": "hidden"}
        elif isinstance(val, (dict, list)):
            out[key] = _memory_safe_profile(val)
        else:
            out[key] = val
    for key in ("email", "phone", "wechat", "id_card"):
        if isinstance(out.get(key), str) and out[key]:
            out[key] = redact_text(out[key])
    return out


@app.get("/api/memory/{user_id}")
def memory_list(
    user_id: str,
    q: str = Query(""),
    kind: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> ApiResponse:
    """Read-only memory inspect for QA: profile snapshot + episodic + prompt context."""
    from app.agent.memory import format_memory_context, get_profile, search_events
    from app.agent.memory import profile as profile_mem

    from app.agent.harness.redact import redact_text

    events = search_events(user_id, query=q, kind=kind, limit=limit)
    ctx = format_memory_context(user_id, query=q, limit=min(limit, 8))
    for ev in events:
        if isinstance(ev, dict) and isinstance(ev.get("summary"), str):
            ev["summary"] = redact_text(ev["summary"], max_len=240)
    return ApiResponse(
        status="memory",
        data={
            "user_id": user_id,
            "profile": _memory_safe_profile(public_profile(get_profile(user_id), user_id=user_id)),
            "profile_snapshot": _memory_safe_profile(public_profile(profile_mem.snapshot(user_id), user_id=user_id)),
            "events": events,
            "context": redact_text(public_profile({"context": ctx}, user_id=user_id)["context"], max_len=1200),
            "layers": {
                "working": "LangGraph SqliteSaver by thread_id",
                "profile": "user_profiles SQLite",
                "episodic": "episodic_events keyword/tag (no vectors)",
            },
        },
    )


@app.get("/api/llm/usage")
def llm_usage(limit: int = Query(50, ge=0, le=500)) -> ApiResponse:
    """De-identified usage metrics; no request IDs or upstream error text."""
    from app.gateway.usage import public_snapshot

    return ApiResponse(status="llm_usage", data=public_snapshot(limit))


def _memory_view(user_id: str, thread_id: Optional[str] = None):
    from app.agent.memory.card import MemoryAccessError, card_view

    try:
        view = card_view(user_id, thread_id=thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.get("/api/memory/{user_id}/card")
def memory_card(
    user_id: str,
    thread_id: Optional[str] = Query(None),
) -> ApiResponse:
    """In-thread memory card. Facts and episodes are redacted and user-scoped."""
    return _memory_view(user_id, thread_id)


@app.patch("/api/memory/{user_id}/facts/{fact_id:path}")
def memory_update_fact(user_id: str, fact_id: str, body: MemoryFactBody) -> ApiResponse:
    from app.agent.memory.card import MemoryAccessError, update_fact

    try:
        view = update_fact(user_id, fact_id, body.value, thread_id=body.thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.delete("/api/memory/{user_id}/facts/{fact_id:path}")
def memory_delete_fact(
    user_id: str,
    fact_id: str,
    thread_id: Optional[str] = Query(None),
) -> ApiResponse:
    from app.agent.memory.card import MemoryAccessError, delete_fact

    try:
        view = delete_fact(user_id, fact_id, thread_id=thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.patch("/api/memory/{user_id}/events/{event_id}")
def memory_update_event(user_id: str, event_id: int, body: MemoryEventBody) -> ApiResponse:
    from app.agent.memory.card import MemoryAccessError, update_episode

    try:
        view = update_episode(user_id, event_id, body.value, thread_id=body.thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.delete("/api/memory/{user_id}/events/{event_id}")
def memory_delete_event(
    user_id: str,
    event_id: int,
    thread_id: Optional[str] = Query(None),
) -> ApiResponse:
    from app.agent.memory.card import MemoryAccessError, delete_episode

    try:
        view = delete_episode(user_id, event_id, thread_id=thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.post("/api/memory/{user_id}/forget")
def memory_forget(user_id: str, body: MemoryForgetBody) -> ApiResponse:
    """Clear profile memory and episodic events after an explicit confirm."""
    from app.agent.memory.card import MemoryAccessError, forget_all

    try:
        view = forget_all(user_id, confirm=body.confirm, thread_id=body.thread_id)
    except MemoryAccessError as exc:
        raise HTTPException(
            exc.http_status,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return ApiResponse(status="memory", data=view)


@app.get("/api/sports/summary")
def sports_summary(user_id: str = Query(""), days: int = Query(28, ge=1, le=365)) -> ApiResponse:
    """Sport profile + workout stats + current plan for the browser (owner only)."""
    from app.sports import store as sport_store

    uid = _require_user(user_id)
    with user_scope(uid):
        return ApiResponse(
            status="sports",
            data={
                "user_id": uid,
                "profile": sport_store.get_profile(uid),
                "stats": sport_store.stats(uid, days=days),
                "plan": sport_store.get_plan(uid),
            },
        )


@app.get("/api/companion")
def companion_status(user_id: str = Query("")) -> ApiResponse:
    """Avatar, energy balance, meals and social state for the browser dashboard."""
    from app.companion import meals as meal_library
    from app.companion import pose, social, vision
    from app.companion import store as companion_store

    uid = _require_user(user_id)
    with user_scope(uid):
        data = companion_store.status(uid)
        data["pose"] = pose.pose_status()
        data["social_privacy"] = social.privacy_contract()
        data["meal_library_size"] = len(meal_library.MEALS)
        data["vision"] = vision.status()
        return ApiResponse(status="companion", data=data)


@app.post("/api/companion/profile")
def companion_profile(body: CompanionProfileRequest) -> ApiResponse:
    from app.companion import store as companion_store

    uid = _require_user(body.user_id)
    with user_scope(uid):
        changed: list[str] = []
        if body.height_cm and body.weight_kg:
            companion_store.set_body(uid, height_cm=body.height_cm, weight_kg=body.weight_kg,
                                     body_fat_pct=body.body_fat_pct, age=body.age, sex=body.sex,
                                     activity_level=body.activity_level, goal=body.goal)
            changed.append("body")
        if body.name:
            from app import db as app_db
            app_db.merge_and_save(uid, {"companion_name": body.name.strip()[:24] or "阿动"})
            changed.append("name")
        if body.social_label:
            companion_store.set_social(uid, body.social_label)
            changed.append("social")
        if not changed:
            raise HTTPException(400, detail={"code": "empty", "message": "没有要保存的内容"})
        return ApiResponse(status="companion", data={"status": companion_store.status(uid), "changed": changed})


@app.post("/api/companion/meal")
def companion_meal(body: CompanionMealRequest) -> ApiResponse:
    from app.companion import store as companion_store

    uid = _require_user(body.user_id)
    with user_scope(uid):
        return ApiResponse(status="companion", data=companion_store.log_meal(
            uid, name=body.name, kcal=body.kcal, protein_g=body.protein_g,
            carbs_g=body.carbs_g, fat_g=body.fat_g, source=body.source,
        ))


@app.post("/api/companion/rest-timer")
def companion_rest_timer(body: RestTimerRequest) -> ApiResponse:
    from app.companion import timer

    card = timer.rest_card(body.seconds, exercise=body.exercise, set_index=body.set_index,
                           sets=body.sets, intensity=body.intensity)
    return ApiResponse(status="rest_timer", data={"card": card, "text": timer.rest_text(card)})


@app.get("/api/companion/report")
def companion_report_preview(user_id: str = Query(""), days: int = Query(7, ge=1, le=30)) -> ApiResponse:
    from app.companion import report

    uid = _require_user(user_id)
    with user_scope(uid):
        return ApiResponse(status="companion_report", data=report.build_report(uid, days=days))


@app.post("/api/companion/report")
def companion_report_send(body: CompanionReportRequest) -> ApiResponse:
    """Manual send now (in-app item + bound channel); the scheduler uses the same builder."""
    from app.companion import report

    uid = _require_user(body.user_id)
    with user_scope(uid):
        built = report.build_report(uid, days=max(1, min(int(body.days or 7), 30)))
        result = report.deliver_report(uid, built)
        return ApiResponse(status="companion_report", data={
            "report": built, "delivery": result.get("delivery") or {}, "item": result.get("item") or {},
        })


@app.post("/api/companion/revive")
def companion_revive(body: CompanionReviveRequest) -> ApiResponse:
    from app.companion import store as companion_store

    uid = _require_user(body.user_id)
    with user_scope(uid):
        return ApiResponse(status="companion", data={"status": companion_store.revive(uid, name=body.name)})


@app.post("/api/companion/food/analyze")
async def companion_food_analyze(file: UploadFile = File(...)) -> ApiResponse:
    """Photo → candidate items. The image stays in memory and is never stored."""
    from app.agent.tool_registry import ToolError
    from app.companion.vision import MAX_BYTES, analyze_food

    payload = await file.read(MAX_BYTES + 1)
    try:
        result = analyze_food(payload, mime=(file.content_type or "image/jpeg").split(";")[0])
    except ToolError as exc:
        status = 501 if exc.code == "vision_not_configured" else 400
        raise HTTPException(status, detail=exc.as_dict()["error"]) from exc
    return ApiResponse(status="food_candidates", data=result)


@app.get("/api/tools")
@app.get("/api/agent/tools")
def agent_tools() -> ApiResponse:
    from app.agent import tool_registry

    return ApiResponse(status="tools", data={"tools": tool_registry.list_tools()})


# ----- Proactive (scheduler wake; default OFF) -----


@app.get("/api/proactive/pending")
def proactive_pending(
    user_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    status: str = Query("pending"),
) -> ApiResponse:
    """List pending (or filtered) proactive items for web/QA fetch."""
    from app.agent.proactive import store as proactive_store

    items = proactive_store.list_pending(user_id, limit=limit, status=status)
    return ApiResponse(
        status="proactive",
        data={"items": items, "count": len(items), "filter_status": status},
    )


@app.post("/api/proactive/tick", dependencies=[Depends(require_scope("users:write"))])
def proactive_tick() -> ApiResponse:
    """Manual one-shot tick (honors PROACTIVE_ENABLED). Admin only: it runs for every user."""
    from app.agent import proactive as proactive_mod

    result = proactive_mod.trigger_tick_now()
    # A system tick can include every user's IDs, summaries and deliveries.
    # The user-facing response exposes counts only.
    data = {key: result[key] for key in ("ok", "skipped", "users", "elapsed_ms") if key in result}
    if "schedules" in result:
        data["schedules"] = len(result["schedules"])
    if result.get("reason") == "disabled":
        data["reason"] = "disabled"
    return ApiResponse(status="tick", data=data)


@app.get("/api/proactive/status")
def proactive_status_api() -> ApiResponse:
    from app.agent import proactive as proactive_mod

    return ApiResponse(status="proactive", data=proactive_mod.status())


@app.post("/api/proactive/items/{item_id}/dismiss")
def proactive_dismiss(item_id: int, user_id: str = Query("")) -> ApiResponse:
    from app.agent.proactive import store as proactive_store

    if proactive_store.item_owner(item_id) != _require_user(user_id):
        raise HTTPException(404, "proactive item not found")
    row = proactive_store.mark_item(item_id, status="dismissed")
    if not row:
        raise HTTPException(404, "proactive item not found")
    return ApiResponse(status="dismissed", data={"item": row})


# ----- Harness: traces / feedback / audit -----


@app.get("/api/traces/{thread_id}", dependencies=[Depends(require_admin)])
def get_traces(
    thread_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> ApiResponse:
    """Harness (admin): per-step traces for a thread."""
    from app.agent.harness.traces import list_traces

    rows = list_traces(thread_id, limit=limit)
    return ApiResponse(
        thread_id=thread_id,
        status="ok" if rows else "empty",
        data={"traces": rows, "count": len(rows)},
    )


@app.get("/api/admin/traces", dependencies=[Depends(require_admin)])
def admin_traces(
    thread_id: str = Query(""),
    user_id: str = Query(""),
    tool: str = Query(""),
    ok: Optional[bool] = Query(None),
    since: Optional[datetime] = Query(None, description="ISO-8601 lower bound on created_at"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    order: str = Query("asc", pattern="^(asc|desc)$"),
    trace_id: str = Query("", description="OTel / Langfuse trace id (X-Trace-Id)"),
) -> ApiResponse:
    """Harness (admin): structured, redacted trajectory log filtered by thread / user.

    ``summary`` aggregates every matching row (``scope="all"``); ``page_summary``
    covers only the returned page. Ids in rows are HMAC pseudonyms; filters accept
    raw ids or pseudonyms.
    """
    from app.agent.harness.traces import page_summary, query_traces, writer_stats

    res = query_traces(
        thread_id=thread_id,
        user_id=user_id,
        tool=tool,
        ok=ok,
        since=since,
        limit=limit,
        offset=offset,
        order=order,
        trace_id=trace_id,
        with_summary=True,
    )
    rows = res["traces"]
    return ApiResponse(
        thread_id=thread_id or None,
        status="ok" if rows else "empty",
        data={
            "traces": rows,
            "count": len(rows),
            "total": res["total"],
            "filters": {
                "thread_id": thread_id,
                "user_id": user_id,
                "tool": tool,
                "ok": ok,
                "since": since.isoformat() if since else None,
                "trace_id": trace_id,
            },
            "summary": res["summary"],
            "page_summary": {**page_summary(rows), "offset": offset, "limit": limit},
            "writer": writer_stats(),
        },
    )


class FeedbackRequest(BaseModel):
    user_id: str
    trace_id: str
    value: str | int
    thread_id: str = ""
    comment: str = ""


@app.post("/api/feedback")
def post_feedback(req: FeedbackRequest) -> ApiResponse:
    """Thumbs up / down on one assistant reply (trace id from the reply's X-Trace-Id)."""
    from app.observability.feedback import FeedbackError, record_feedback

    try:
        row = record_feedback(
            user_id=req.user_id, trace_id=req.trace_id, value=req.value, comment=req.comment, thread_id=req.thread_id
        )
    except FeedbackError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    return ApiResponse(status="ok", data={"id": row["id"], "value": row["value"], "langfuse": row["langfuse"]})


@app.get("/api/admin/feedback", dependencies=[Depends(require_admin)])
def admin_feedback(
    value: Optional[int] = Query(None, description="1 or -1"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    from app.observability.feedback import list_feedback

    res = list_feedback(value=value, limit=limit, offset=offset)
    return ApiResponse(status="ok" if res["items"] else "empty", data=res)


class ToDatasetRequest(BaseModel):
    dataset: str = ""
    note: str = ""


@app.post("/api/admin/feedback/{feedback_id}/to-dataset")
def admin_feedback_to_dataset(
    feedback_id: int,
    req: Optional[ToDatasetRequest] = None,
    principal: AdminPrincipal = Depends(require_scope("feedback:write")),
) -> ApiResponse:
    """One click: the rated trace becomes a Langfuse dataset item (offline: a golden-case candidate file)."""
    from app.observability.feedback import FeedbackError, add_to_dataset

    req = req or ToDatasetRequest()
    try:
        res = add_to_dataset(feedback_id, dataset=req.dataset, note=req.note, principal=principal)
    except FeedbackError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"code": "dataset_failed", "message": type(exc).__name__})
    return ApiResponse(status="ok", data=res)


@app.get("/api/admin/audit", dependencies=[Depends(require_scope("audit:read"))])
def admin_audit(
    principal: str = Query(""),
    outcome: str = Query("", pattern="^(|allowed|denied|forbidden|disabled)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ApiResponse:
    """Harness (admin): who called which admin endpoint, including denied attempts."""
    from app.agent.harness.admin import query_audit

    res = query_audit(principal=principal, outcome=outcome, limit=limit, offset=offset)
    return ApiResponse(status="ok" if res["events"] else "empty", data=res)


# ----- Channel binding (admin-only; users bind through the chat → /bind flow) -----


def _require_admin(request: Request) -> None:
    import hmac

    from app.channels.settings import admin_token

    expected = admin_token()
    if not expected:
        raise HTTPException(403, "admin binding API disabled (set CHANNEL_ADMIN_TOKEN)")
    auth = request.headers.get("authorization") or ""
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    got = got or request.headers.get("x-admin-token") or ""
    if not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(401, "invalid admin token")


@app.post("/api/channels/auto-bind")
@system_scope()
def api_auto_bind(request: Request) -> ApiResponse:
    """Re-apply FEISHU_BIND_OPEN_IDS / CHANNEL_AUTO_BIND from env."""
    _require_admin(request)
    rows = apply_auto_bindings()
    return ApiResponse(status="bound", data={"bindings": rows, "count": len(rows)})


@app.post("/api/channels/bind")
@system_scope()
def api_bind_channel(body: BindChannelRequest, request: Request) -> ApiResponse:
    _require_admin(request)
    try:
        row = bind_channel(
            body.channel,
            body.external_user_id,
            body.user_id,
            display_name=body.display_name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return ApiResponse(status="bound", data={"binding": row})


@app.get("/api/channels/bindings")
@system_scope()
def api_list_bindings(
    request: Request,
    user_id: Optional[str] = Query(None),
) -> ApiResponse:
    _require_admin(request)
    return ApiResponse(
        status="ok", data={"bindings": list_bindings(user_id)}
    )


@app.delete("/api/channels/bind")
@system_scope()
def api_unbind(
    request: Request,
    channel: str = Query(...),
    external_user_id: str = Query(...),
) -> ApiResponse:
    _require_admin(request)
    ok = unbind_channel(channel, external_user_id)
    if not ok:
        raise HTTPException(404, "binding not found")
    return ApiResponse(status="unbound", data={"channel": channel})


@app.get("/api/channels")
def api_channels() -> dict[str, Any]:
    from app.channels.registry import channels_status

    return {"ok": True, "channels": channels_status()}


# ----- Conversational bind + privacy sandbox (web side of channel links) -----


class BindCompleteRequest(BaseModel):
    token: str


class SandboxSubmitRequest(BaseModel):
    token: str
    phone: str = ""
    email: str = ""
    name: str = ""
    city: str = ""
    facts: str = ""


@system_scope()
def _link_or_410(token: str, purpose: str) -> dict[str, Any]:
    from app.channels import links

    row = links.peek(token, purpose)
    if not row:
        raise HTTPException(410, {"code": "link_invalid", "message": "链接已失效，请回到对话里重新获取"})
    return row


@app.get("/api/channels/link")
def api_channel_link(t: str = Query(...), purpose: str = Query(...)) -> dict[str, Any]:
    """Describe a channel link for the web page (no secrets, nothing consumed)."""
    from app.channels.gateway import _channel_label

    row = _link_or_410(t, purpose)
    return {
        "ok": True,
        "purpose": row["purpose"],
        "channel": row["channel"],
        "channel_label": _channel_label(row["channel"]),
        "display_name": row["display_name"],
        "expires_at": row["expires_at"],
    }


@app.post("/api/channels/bind/complete")
@system_scope()
def api_bind_complete(body: BindCompleteRequest, request: Request) -> dict[str, Any]:
    """Bind the channel account in the link to the browser session that opened it."""
    from app.channels import links
    from app.channels.base import ChannelReply
    from app.channels.gateway import _channel_label, _mask, notify

    uid = session_user(request)
    if not uid:
        raise HTTPException(401, {"code": "session_required", "message": "会话已失效，请刷新页面后再点确认绑定"})
    row = _link_or_410(body.token, "bind")
    if not links.consume(body.token, "bind"):
        raise HTTPException(410, {"code": "link_invalid", "message": "链接已失效，请回到对话里重新获取"})
    try:
        bind_channel(row["channel"], row["external_user_id"], uid, display_name=row["display_name"])
    except ValueError as exc:
        raise HTTPException(400, {"code": "bind_rejected", "message": str(exc)}) from exc
    label = _channel_label(row["channel"])
    delivery = notify(
        row["channel"],
        row["external_user_id"],
        ChannelReply(
            text=(
                f"绑定成功！这个{label}账号已关联个人档案 {_mask(uid)}。\n"
                "现在可以直接和我聊，或回复「帮助」看看能做什么。"
            )
        ),
    )
    return {
        "ok": True,
        "status": "bound",
        "channel": row["channel"],
        "channel_label": label,
        "user_id": _mask(uid),
        "notified": bool(delivery.get("ok")),
    }


@app.post("/api/sandbox/submit")
def api_sandbox_submit(body: SandboxSubmitRequest) -> dict[str, Any]:
    """Private data typed on the web page → profile of the bound user. Never echoed back to IM."""
    from app.channels import links
    from app.channels.base import ChannelReply
    from app.channels.gateway import notify
    from app.channels.privacy import detect_pii

    phone = (body.phone or "").strip()
    email = (body.email or "").strip()
    name = (body.name or "").strip()
    city = (body.city or "").strip()
    facts = [x.strip() for x in (body.facts or "").splitlines() if x.strip()]
    if not (phone or email or name or city or facts):
        raise HTTPException(400, {"code": "empty", "message": "请至少填写一项"})
    if phone and "mobile" not in detect_pii(phone):
        raise HTTPException(400, {"code": "bad_phone", "message": "手机号格式不正确"})
    if email and "email" not in detect_pii(email):
        raise HTTPException(400, {"code": "bad_email", "message": "邮箱格式不正确"})
    with system_scope():
        row = links.consume(body.token, "sandbox")
    if not row or not row.get("user_id"):
        raise HTTPException(410, {"code": "link_invalid", "message": "链接已失效，请回到对话里重新获取"})
    uid = row["user_id"]
    from app.data_access import user_scope
    with user_scope(uid):
        patch: dict[str, Any] = {k: v for k, v in (("phone", phone), ("email", email), ("name", name), ("city", city)) if v}
        if facts:
            patch["facts"] = facts[:20]
        db.merge_and_save(uid, patch)
        labels = {"phone": "手机号", "email": "邮箱", "name": "姓名", "city": "城市", "facts": "备注"}
        updated = [labels[k] for k in patch]
        notify(
            row["channel"],
            row["external_user_id"],
            ChannelReply(
                text=(
                    f"已通过网页安全通道收到你的{'、'.join(updated)}，档案已更新。"
                    "隐私内容只保存在服务端，不会发到这里。"
                )
            ),
        )
        return {"ok": True, "status": "profile_updated", "updated": updated}


@app.get("/api/sandbox/draft")
def api_sandbox_draft(t: str = Query(...)) -> dict[str, Any]:
    """Full draft behind a one-time link (kept out of IM messages)."""
    row = _link_or_410(t, "draft")
    from app.data_access import user_scope
    with user_scope(row["user_id"]):
        thread_id = str((row.get("payload") or {}).get("thread_id") or "")
        try:
            snap = require_confirmable_thread(thread_id, row["user_id"])
        except ConfirmThreadError as exc:
            raise HTTPException(exc.http_status, {"code": exc.code, "message": exc.message}) from exc
        values = snap.get("values") or {}
        draft = dict(values.get("draft") or {})
        decision = confirm_store.get_decision(thread_id)
        return {
            "ok": True,
            "thread_id": thread_id,
            "kind": draft.get("kind") or "other",
            "title": draft.get("title") or "",
            "body": draft.get("body") or "",
            "notes": draft.get("notes") or [],
            "decision": decision,
        }


@app.get("/bind", include_in_schema=False)
def bind_page():
    return _versioned_page("bind.html")


@app.get("/sandbox", include_in_schema=False)
def sandbox_page():
    return _versioned_page("sandbox.html")


# ----- Channel ingress: Feishu (long connection preferred; HTTP fallback) + WeChat (OpenClaw) -----

_channel_pool = None


def _pool():
    global _channel_pool
    if _channel_pool is None:
        from concurrent.futures import ThreadPoolExecutor

        _channel_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="channel")
    return _channel_pool


def _handle_channel_inbound(parsed: dict[str, Any], default_channel: str) -> dict[str, Any]:
    """All channel ingress → shared gateway → agent → rendered reply."""
    from app.channels.gateway import handle_inbound

    return handle_inbound(parsed, default_channel)


def _maybe_deliver(channel: str, inbound: dict[str, Any], result: dict[str, Any], *, callback_url: str = "") -> dict[str, Any]:
    from app.channels.gateway import deliver_result

    return deliver_result(channel, inbound, result, callback_url=callback_url)


def _process_and_deliver(parsed: dict[str, Any], channel: str, callback_url: str = "") -> dict[str, Any]:
    result = _handle_channel_inbound(parsed, channel)
    inbound = parsed.get("inbound") or {}
    return _maybe_deliver(channel, inbound if isinstance(inbound, dict) else {}, result, callback_url=callback_url)


@app.post("/webhooks/feishu")
async def webhook_feishu(request: Request) -> dict[str, Any]:
    """HTTP fallback; production uses the long connection (no public URL)."""
    from app.channels import feishu_client
    from app.channels.registry import get_adapter

    if not get_adapter("feishu").enabled():
        raise HTTPException(503, "feishu_disabled")
    if not (feishu_client.verification_token() or feishu_client.encrypt_key()):
        raise HTTPException(503, "FEISHU_VERIFICATION_TOKEN or FEISHU_ENCRYPT_KEY required for HTTP webhook")
    raw = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    try:
        import json as _json

        payload = _json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    parsed = parse_feishu_webhook(
        payload if isinstance(payload, dict) else {},
        raw_body=raw,
        headers=headers,
    )
    if parsed.get("type") == "challenge":
        return {"challenge": parsed.get("challenge", "")}
    if parsed.get("type") == "error":
        raise HTTPException(int(parsed.get("status_code") or 400), parsed.get("error") or "feishu_error")
    # Feishu expects a reply within 3s; agent work continues in the pool.
    _pool().submit(_process_and_deliver, parsed, "feishu")
    return {"ok": True, "status": "accepted"}


@app.post("/webhooks/openclaw/wechat")
async def webhook_openclaw_wechat(request: Request) -> dict[str, Any]:
    """Called by the OpenClaw bridge plugin (before_dispatch hook).

    Waits up to ``sync_wait_seconds`` and returns the reply for OpenClaw to send
    verbatim. Slower turns return an ack; the final reply is pushed through
    Gateway ``/tools/invoke`` (needs OPENCLAW_GATEWAY_URL/TOKEN).
    """
    import asyncio

    from app.channels import openclaw_wechat as oc
    from app.channels.registry import get_adapter
    from app.channels.settings import wechat_enabled

    if not wechat_enabled():
        raise HTTPException(503, "wechat_disabled")
    if not oc.webhook_token():
        raise HTTPException(503, "OPENCLAW_WEBHOOK_TOKEN required")
    if not oc.verify_inbound_auth({k: v for k, v in request.headers.items()}):
        raise HTTPException(401, "invalid_openclaw_token")
    payload = await request.json()
    payload = payload if isinstance(payload, dict) else {}
    parsed = parse_openclaw_webhook(payload)
    inbound = parsed.get("inbound") or {}
    callback_url = str(parsed.get("callback_url") or "")

    if callback_url:
        _pool().submit(_process_and_deliver, parsed, "openclaw_wechat", callback_url)
        return {"ok": True, "status": "accepted", "reply": {"text": ""}}

    future = _pool().submit(_handle_channel_inbound, parsed, "openclaw_wechat")
    wait = float(payload.get("sync_wait_seconds") or oc.sync_wait_seconds())
    if not oc.can_push():
        wait = None  # no async push path → must answer synchronously
    try:
        result = await asyncio.wait_for(asyncio.wrap_future(future), timeout=wait)
        return result
    except asyncio.TimeoutError:
        adapter = get_adapter("openclaw_wechat")

        def _push_later(fut) -> None:
            try:
                res = fut.result()
                _maybe_deliver("openclaw_wechat", inbound, res)
            except Exception:
                import logging

                logging.getLogger("channels").exception("openclaw async push failed")

        future.add_done_callback(_push_later)
        return {
            "ok": True,
            "status": "accepted_async",
            "reply": {"text": "收到，正在处理，稍等我一下～"},
            "push_via": "openclaw_tools_invoke" if adapter and adapter.enabled() else None,
        }


_VERSIONED_SUFFIXES = (".html", ".js", ".css")


def _versioned_text(path: Path) -> str:
    """Single source of truth: VERSION file → every __APP_VERSION__ token in static assets."""
    return path.read_text(encoding="utf-8").replace(VERSION_TOKEN, __version__)


class VersionedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):  # type: ignore[override]
        resp = await super().get_response(path, scope)
        if resp.status_code == 200 and path.endswith(_VERSIONED_SUFFIXES) and isinstance(resp, FileResponse):
            from fastapi.responses import Response as _Resp

            media = resp.media_type or "text/plain"
            return _Resp(_versioned_text(Path(resp.path)), media_type=media, headers={"Cache-Control": "no-cache"})
        return resp


def _versioned_page(name: str):
    from fastapi.responses import HTMLResponse

    page = STATIC / name
    if not page.exists():
        raise HTTPException(404, f"static/{name} missing")
    return HTMLResponse(_versioned_text(page))


if STATIC.exists():
    app.mount("/static", VersionedStaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index():
    return _versioned_page("index.html")


def main() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8787"))
    uvicorn.run("app.main:app", host=host, port=port, reload=False)


@app.get("/admin", include_in_schema=False)
@app.get("/admin/{path:path}", include_in_schema=False)
def admin_spa(path: str = ""):
    from app.agent.harness.admin import admin_configured

    if not admin_configured():
        raise HTTPException(503, "管理后台未启用")
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer", "Content-Security-Policy":
               "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
               "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"}
    if path in {"admin.js", "admin.css"}:
        return FileResponse(STATIC / "admin" / path, headers=headers)
    if path and path not in {"users", "routes", "prompts", "audit", "login"}:
        raise HTTPException(404)
    return FileResponse(STATIC / "admin" / "index.html", headers=headers)


@app.api_route("/api/admin/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
               dependencies=[Depends(require_scope(""))], include_in_schema=False)
def admin_unknown(path: str):
    raise HTTPException(404, "管理接口不存在")


if __name__ == "__main__":
    main()


from app.agent.schedules.api import router as schedules_router
app.include_router(schedules_router)
