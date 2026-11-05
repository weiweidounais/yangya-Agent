"""Unified channel pipeline: inbound → (bind / privacy / options) → agent → reply.

Every adapter feeds the same normalized inbound dict into ``handle_inbound``
and gets back a channel-neutral ``ChannelReply`` plus its rendered form.
Order of checks matters:

  1. Group chats are refused (no personal data in groups).
  2. Unbound users only get the bind flow — never a demo or default account.
  3. Option replies (card click, ``1``/``2``, ``确认``) resolve against the menu.
  4. Private data is never accepted in the channel; users get a web sandbox link.
  5. Everything else goes to the same LangGraph agent used by the web app.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from app.channels import conversation, links
from app.channels.base import ChannelLink, ChannelOption, ChannelReply
from app.channels.binding import get_binding, unbind_channel
from app.channels.outbound import build_confirm_reply, guard_rendered, send_reply
from app.channels.privacy import (
    asks_for_private_info,
    describe,
    detect_pii,
    redact,
    redact_inbound,
)
from app.channels.registry import canonical_name, get_adapter
from app.channels.settings import (
    bind_link_ttl,
    is_reserved_user_id,
    public_base_url,
    sandbox_link_ttl,
)

logger = logging.getLogger("channels.gateway")

_CMD_BIND = {"绑定", "绑定账号", "bind", "/bind"}
_CMD_UNBIND = {"解绑", "解除绑定", "unbind", "/unbind"}
_CMD_STATUS = {"我是谁", "绑定状态", "whoami", "/whoami"}
_CMD_SANDBOX = {"隐私", "安全通道", "网页填写", "更新档案", "sandbox", "/sandbox"}
_CONFIRM_WORDS = {"CONFIRM": True, "确认": True, "REJECT": False, "拒绝": False}

@dataclass
class _Ctx:
    channel: str
    external: str
    uid: Optional[str]
    inbound: dict[str, Any]
    display_name: str = ""
    intent_state: Optional[dict[str, Any]] = None


def _link(path: str, token: str) -> str:
    return f"{public_base_url()}{path}?t={token}"


def issue_bind_link(channel: str, external_user_id: str, display_name: str = "") -> str:
    links.revoke_for(channel, external_user_id, "bind")
    token = links.issue(
        "bind",
        channel=channel,
        external_user_id=external_user_id,
        display_name=display_name,
        ttl_seconds=bind_link_ttl(),
    )
    return _link("/bind", token)


def issue_sandbox_link(channel: str, external_user_id: str, user_id: str) -> str:
    links.revoke_for(channel, external_user_id, "sandbox")
    token = links.issue(
        "sandbox",
        channel=channel,
        external_user_id=external_user_id,
        user_id=user_id,
        ttl_seconds=sandbox_link_ttl(),
    )
    return _link("/sandbox", token)


def issue_draft_link(channel: str, external_user_id: str, user_id: str, thread_id: str) -> str:
    token = links.issue(
        "draft",
        channel=channel,
        external_user_id=external_user_id,
        user_id=user_id,
        payload={"thread_id": thread_id},
        ttl_seconds=sandbox_link_ttl(),
    )
    return _link("/sandbox", token)


def _channel_label(channel: str) -> str:
    adapter = get_adapter(channel)
    return adapter.display_name if adapter else channel


def _mask(uid: str) -> str:
    uid = str(uid or "")
    return "****" if len(uid) <= 4 else f"{uid[:2]}***{uid[-2:]}"


def extract_thread_id(inbound: dict[str, Any]) -> str:
    """Pull thread_id from adapter fields or nested card/raw payload."""
    if inbound.get("thread_id"):
        return str(inbound["thread_id"])
    value = inbound.get("action_value") if isinstance(inbound.get("action_value"), dict) else {}
    if value.get("thread_id"):
        return str(value["thread_id"])
    raw = inbound.get("raw") if isinstance(inbound.get("raw"), dict) else {}
    if raw.get("_thread_id"):
        return str(raw["_thread_id"])
    for src in (
        raw,
        raw.get("action") if isinstance(raw.get("action"), dict) else {},
        raw.get("data") if isinstance(raw.get("data"), dict) else {},
        raw.get("openclaw") if isinstance(raw.get("openclaw"), dict) else {},
    ):
        val = src.get("value") if isinstance(src.get("value"), dict) else src
        if isinstance(val, dict) and val.get("thread_id"):
            return str(val["thread_id"])
    return ""


# ----- replies that do not touch the agent -----


def _bind_reply(ctx: _Ctx) -> ChannelReply:
    url = issue_bind_link(ctx.channel, ctx.external, ctx.display_name)
    minutes = max(1, bind_link_ttl() // 60)
    return ChannelReply(
        title="先绑定账号",
        kind="bind",
        text=(
            f"你好！为了把{_channel_label(ctx.channel)}里的对话和你的个人档案对上，需要先绑定一下。\n"
            f"请用常用浏览器打开下方链接，点「确认绑定」即可（{minutes} 分钟内有效，只能用一次）。"
            "绑定后，这里和网页用的是同一份档案和记忆。\n"
            "链接过期了就回复「绑定」重新获取。"
        ),
        links=[ChannelLink(label="打开网页完成绑定", url=url)],
    )


def _sandbox_reply(ctx: _Ctx, *, reason: str) -> ChannelReply:
    url = issue_sandbox_link(ctx.channel, ctx.external, str(ctx.uid))
    minutes = max(1, sandbox_link_ttl() // 60)
    return ChannelReply(
        title="请在网页安全通道填写",
        kind="privacy",
        text=(
            f"{reason}\n"
            f"手机号、邮箱这类隐私信息不在{_channel_label(ctx.channel)}里收集。"
            f"请点击下方链接，在网页安全通道里填写（{minutes} 分钟内有效）。"
        ),
        links=[ChannelLink(label="打开网页安全通道", url=url)],
    )


def _draft_confirm_reply(ctx: _Ctx, thread_id: str, draft: dict[str, Any]) -> ChannelReply:
    draft_url = issue_draft_link(ctx.channel, ctx.external, str(ctx.uid), thread_id)
    return build_confirm_reply(
        title=str(draft.get("title") or ""),
        body=str(draft.get("body") or ""),
        kind=str(draft.get("kind") or "other"),
        thread_id=thread_id,
        draft_url=draft_url,
    )


# ----- actions -----


def _do_confirm(
    ctx: _Ctx, thread_id: str, confirmed: bool, reason: str | None = None
) -> tuple[ChannelReply, str, dict[str, Any]]:
    from app.main import ConfirmThreadError, _apply_confirm

    if not thread_id:
        return ChannelReply(text="没找到要确认的草稿。先告诉我要写什么，我出稿后再确认。"), "need_thread", {}
    try:
        result = _apply_confirm(
            user_id=str(ctx.uid),
            thread_id=thread_id,
            confirmed=confirmed,
            channel=ctx.channel,
            reason=None if confirmed else reason,
        )
    except ConfirmThreadError as exc:
        return ChannelReply(text=exc.message), exc.code, {}

    data = result.get("data") or {}
    if result.get("awaiting_confirm"):
        reply = _draft_confirm_reply(ctx, thread_id, data.get("draft") or {})
        note = data.get("side_effect_note")
        if note:
            reply.text = f"{note}\n\n{reply.text}"
        return reply, result["status"], {"result": result, "thread_id": thread_id}

    note = str(data.get("side_effect_note") or ("已确认" if confirmed else "已拒绝"))
    if confirmed and (data.get("draft") or {}).get("body"):
        body = redact(str(data["draft"]["body"]))[:1500]
        note = f"{note}\n\n{body}"
    reply = ChannelReply(text=note)
    conversation.clear_pending(ctx.channel, ctx.external)
    return reply, result["status"], {"result": result, "thread_id": thread_id}


def _do_draft(ctx: _Ctx, text: str) -> tuple[ChannelReply, str, dict[str, Any]]:
    from app.graph import new_thread_id, run_action

    out = run_action(
        {"user_id": ctx.uid, "action": "draft", "message": text, "draft_request": text, "channel": ctx.channel},
        thread_id=new_thread_id(),
    )
    state = out.get("state") or {}
    tid = out["thread_id"]
    extra = {"thread_id": tid, "intent": "draft"}
    if not out.get("awaiting_confirm"):
        msgs = state.get("messages") or []
        text = str((msgs[-1] or {}).get("content") or "") if msgs else ""
        return ChannelReply(text=text or "已处理。"), "done", extra
    draft = dict(state.get("draft") or {})
    interrupt = out.get("interrupt") or {}
    if not draft.get("body") and isinstance(interrupt, dict):
        draft["body"] = interrupt.get("body") or ""
        draft["title"] = interrupt.get("draft_title") or draft.get("title") or ""
    return _draft_confirm_reply(ctx, tid, draft), "awaiting_confirm", extra


def _do_agent(ctx: _Ctx, intent: str, text: str) -> tuple[ChannelReply, str, dict[str, Any]]:
    from app.graph import new_thread_id, run_action

    payload: dict[str, Any] = {"user_id": ctx.uid, "action": intent, "message": text, "channel": ctx.channel, "error": None}
    if ctx.intent_state:
        payload.update({k: ctx.intent_state[k] for k in ("intent_result", "intent_message")})
    # Chit-chat shares one per-user thread across channels.
    tid = conversation.conversation_thread_id(str(ctx.uid)) if intent == "chat" else new_thread_id()
    out = run_action(payload, thread_id=tid)
    state = out.get("state") or {}
    msgs = state.get("messages") or []
    reply = ChannelReply(text=str((msgs[-1] or {}).get("content") or "") if msgs else "")
    if state.get("ask"):
        from app.agent.ask_cards import as_plain_text

        reply.text = "\n\n".join(x for x in (reply.text, as_plain_text(state["ask"])) if x)
    if not reply.text:
        reply.text = "已处理。"
    return reply, intent, {
        "thread_id": out.get("thread_id"),
        "intent": intent,
    }


# ----- dispatcher -----


def _command_value(ctx: _Ctx, text: str) -> dict[str, Any]:
    value = ctx.inbound.get("action_value")
    if isinstance(value, dict) and value.get("command"):
        return dict(value)
    from app.agent.router import parse_reject_command

    reason = parse_reject_command(text)
    upper = text.upper()
    if upper in _CONFIRM_WORDS or text in _CONFIRM_WORDS or reason:
        confirmed = False if reason else _CONFIRM_WORDS.get(upper, _CONFIRM_WORDS.get(text))
        thread_id = extract_thread_id(ctx.inbound)
        if not thread_id:
            pending = conversation.get_pending(ctx.channel, ctx.external)
            if pending.get("kind") == "confirm":
                thread_id = str(pending.get("thread_id") or "")
        out = {"command": "CONFIRM" if confirmed else "REJECT", "thread_id": thread_id}
        if reason:
            out["reason"] = reason
        return out
    matched = conversation.match_pending(ctx.channel, ctx.external, text)
    return matched or {}


def _dispatch(ctx: _Ctx, text: str) -> tuple[ChannelReply, str, dict[str, Any]]:
    from app.agent.router import route_intent

    lowered = text.lower()

    if ctx.inbound.get("is_group"):
        return (
            ChannelReply(text="群聊里我不处理个人信息。请私聊我，或回复「帮助」看看能做什么。"),
            "group_blocked",
            {"guardrail": "group_no_personal_data"},
        )

    if not ctx.uid:
        status = "bind_link" if lowered in _CMD_BIND else "need_bind"
        return _bind_reply(ctx), status, {}

    if lowered in _CMD_BIND or lowered in _CMD_STATUS:
        return (
            ChannelReply(
                text=(
                    f"这个{_channel_label(ctx.channel)}账号已绑定个人档案 {_mask(str(ctx.uid))}。"
                    "要换绑请先回复「解绑」，再回复「绑定」。"
                )
            ),
            "already_bound",
            {},
        )

    if lowered in _CMD_UNBIND:
        return (
            ChannelReply(
                title="确认解绑？",
                kind="options",
                text="解绑后这里的对话不会再关联你的个人档案，需要重新绑定才能继续使用。",
                options=[
                    ChannelOption(id="unbind", label="确认解绑", style="danger", value={"command": "UNBIND"}),
                    ChannelOption(id="cancel", label="取消", value={"command": "CANCEL"}),
                ],
            ),
            "unbind_confirm",
            {},
        )

    if lowered in _CMD_SANDBOX:
        return _sandbox_reply(ctx, reason="好的，给你一个网页安全通道。"), "sandbox_link", {}

    value = _command_value(ctx, text)
    command = str(value.get("command") or "").upper()
    if command in ("CONFIRM", "REJECT"):
        return _do_confirm(
            ctx,
            str(value.get("thread_id") or extract_thread_id(ctx.inbound)),
            command == "CONFIRM",
            reason=str(value.get("reason") or "") or None,
        )
    if command == "UNBIND":
        conversation.clear_pending(ctx.channel, ctx.external)
        unbind_channel(ctx.channel, ctx.external)
        return ChannelReply(text="已解绑。需要时回复「绑定」重新绑定。"), "unbound", {}
    if command == "CANCEL":
        conversation.clear_pending(ctx.channel, ctx.external)
        return ChannelReply(text="好的，已取消。"), "cancelled", {}
    if command == "SANDBOX":
        return _sandbox_reply(ctx, reason="给你一个网页安全通道。"), "sandbox_link", {}

    if not text:
        return ChannelReply(text="收到。想做什么可以直接说，或回复「帮助」。"), "empty", {}

    from app.privacy import redact_sensitive, suggest_secret_key
    from app.secrets_vault import CATALOG

    redacted, hits = redact_sensitive(text, include_contact=False)
    if hits:
        label = CATALOG[suggest_secret_key(redacted, hits)]["label"]
        return (
            _sandbox_reply(ctx, reason=f"内容里有敏感信息，我没有保存也不会处理这条消息。需要填写「{label}」请使用网页安全输入区。"),
            "privacy_redirect",
            {"pii": hits},
        )

    pii = detect_pii(text)
    if pii:
        return (
            _sandbox_reply(ctx, reason=f"你发的内容里有{describe(pii)}，我没有保存也不会处理这条消息。"),
            "privacy_redirect",
            {"pii": pii},
        )

    intent_state = {"user_id": ctx.uid, "message": text, "action": ""}
    intent = route_intent(intent_state, text=text)
    ctx.intent_state = intent_state
    if intent == "update_profile":
        return _sandbox_reply(ctx, reason="档案请在网页安全通道里更新。"), "privacy_redirect", {"pii": ["profile"]}
    if intent == "draft":
        return _do_draft(ctx, text)
    return _do_agent(ctx, intent, text)


def _finalize(ctx: _Ctx, reply: ChannelReply) -> ChannelReply:
    reply.text = redact(reply.text)
    if ctx.uid and reply.kind not in ("privacy", "bind") and asks_for_private_info(reply.text):
        url = issue_sandbox_link(ctx.channel, ctx.external, str(ctx.uid))
        reply.text += "\n\n（联系方式等隐私信息请走网页安全通道，不要直接发在这里。）"
        reply.links.append(ChannelLink(label="打开网页安全通道", url=url))
    if reply.options:
        conversation.set_pending(
            ctx.channel,
            ctx.external,
            kind=reply.kind if reply.kind != "text" else "options",
            options=reply.options,
            thread_id=reply.thread_id,
        )
    return reply


def handle_inbound(parsed: dict[str, Any], default_channel: str) -> dict[str, Any]:
    from app.data_access import user_scope, system_scope
    from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id
    inbound = dict(parsed.get("inbound") or {})
    channel = canonical_name(str(inbound.get("channel") or default_channel))
    uid = resolve_user_id(channel, str(inbound.get("external_user_id") or ""))
    parsed = {**parsed, "inbound": {**inbound, "user_id": uid}}
    if uid:
        with user_scope(uid):
            return _handle_inbound(parsed, default_channel)
    with system_scope():
        return _handle_inbound(parsed, default_channel)


def _handle_inbound(parsed: dict[str, Any], default_channel: str) -> dict[str, Any]:
    """Shared entry for every channel. Returns result dict incl. rendered outbound."""
    if parsed.get("type") == "challenge":
        return parsed
    inbound = dict(parsed.get("inbound") or {})
    channel = canonical_name(str(inbound.get("channel") or default_channel))
    uid = inbound.get("user_id") or None
    if is_reserved_user_id(uid):
        uid = None
    if uid:
        from app.agent.schedules.activity import heartbeat
        heartbeat(uid)
    ctx = _Ctx(
        channel=channel,
        external=str(inbound.get("external_user_id") or ""),
        uid=uid,
        inbound=inbound,
        display_name=str(inbound.get("display_name") or ""),
    )
    text = str(inbound.get("text") or "").strip()
    try:
        reply, status, extra = _dispatch(ctx, text)
        ok = status not in ("need_thread", "thread_not_found", "thread_forbidden")
    except Exception as exc:
        logger.exception("channel dispatch failed channel=%s", channel)
        reply, status, extra, ok = (
            ChannelReply(text="抱歉，处理时出了点问题，请稍后再试。"),
            "error",
            {"error": type(exc).__name__},
            False,
        )
    reply = _finalize(ctx, reply)
    adapter = get_adapter(channel)
    rendered = adapter.render(reply) if adapter and not adapter.reserved else {"text": reply.text, "card": None}
    result: dict[str, Any] = {
        "ok": ok,
        "status": status,
        "message": reply.text,
        "kind": parsed.get("kind"),
        "inbound": redact_inbound(inbound),
        "reply": {"text": rendered.get("text") or "", "card": rendered.get("card")},
        "reply_struct": reply.as_dict(),
        "outbound": {
            "channel": channel,
            "external_user_id": ctx.external,
            "text": rendered.get("text") or "",
            "needs_confirm": reply.needs_confirm,
            "card": rendered.get("card"),
            "user_id": uid,
        },
    }
    result.update(extra)
    return result


def deliver_result(
    channel: str,
    inbound: dict[str, Any],
    result: dict[str, Any],
    *,
    callback_url: str = "",
) -> dict[str, Any]:
    """Delivery may run after ingress returns, including in a future callback."""
    from app.data_access import user_scope, system_scope
    from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id
    outbound = result.get("outbound") or {}
    external = str(outbound.get("external_user_id") or inbound.get("external_user_id") or "")
    uid = resolve_user_id(canonical_name(channel), external)
    with user_scope(uid) if uid else system_scope():
        return _deliver_result(channel, {**inbound, "user_id": uid}, result, callback_url=callback_url)


def _deliver_result(channel, inbound, result, *, callback_url=""):
    outbound = result.get("outbound")
    if not isinstance(outbound, dict):
        return result
    adapter = get_adapter(channel)
    rendered = guard_rendered(
        {"text": outbound.get("text") or "", "card": outbound.get("card")},
        user_id=str(inbound.get("user_id") or ""),
        inputs=(str(inbound.get("text") or ""),),
    )
    outbound.update(rendered)
    result["reply"] = dict(rendered)
    if adapter is None:
        result["delivery"] = {"ok": False, "skipped": True, "reason": f"no_adapter_for_{channel}"}
    else:
        result["delivery"] = adapter.send(
            external_user_id=str(outbound.get("external_user_id") or inbound.get("external_user_id") or ""),
            rendered=rendered,
            message_id=str(inbound.get("message_id") or ""),
            context={"callback_url": callback_url, "account_id": inbound.get("account_id") or ""},
        )
    return result


def notify(channel: str, external_user_id: str, reply: ChannelReply) -> dict[str, Any]:
    """Out-of-band push (bind success, sandbox received). Privacy-redacted."""
    reply.text = redact(reply.text)
    return send_reply(canonical_name(channel), external_user_id, reply)


def binding_summary(channel: str, external_user_id: str) -> Optional[dict[str, Any]]:
    b = get_binding(canonical_name(channel), external_user_id)
    if not b:
        return None
    return {**b, "user_id": _mask(b["user_id"])}
