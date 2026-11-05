"""WeChat via OpenClaw Gateway (Tencent ``@tencent-weixin/openclaw-weixin`` plugin).

How OpenClaw is wired (researched against docs.openclaw.ai):

  * WeChat login / iLink long-poll / context tokens live in the Tencent plugin
    inside the Gateway (channel id ``openclaw-weixin``; aliases ``weixin``, ``wechat``).
  * Inbound: our bridge plugin (``integrations/openclaw-personal-agent-bridge``)
    hooks ``before_dispatch`` — the Gateway's claim point before the model
    runs — and POSTs the message to ``/webhooks/openclaw/wechat``. It returns
    ``{handled: true, text}`` so OpenClaw sends our reply verbatim and its own
    agent never answers.
  * Outbound push (async replies, bind success, proactive reminder): Gateway
    ``POST /tools/invoke`` with the ``message`` tool (``action=send``). Unlike
    ``/hooks/agent`` this does not route through an OpenClaw LLM, so text is
    delivered unchanged. Requires ``group:messaging`` in the session tool policy.

Inbound JSON (bridge plugin; legacy keys also accepted):
  {
    "channel": "openclaw-weixin",
    "from" | "sender_id" | "user_id" | "wxid": "<external id>",
    "sender_name": "...",
    "text" | "content" | "body" | "message": "<text>",
    "msg_id" | "message_id" | "id": "...",
    "conversation_id" | "chat_id" | "room_id": "...",
    "account_id": "<OpenClaw WeChat account>",
    "is_group": false,
    "sync_wait_seconds": 12,
    "callback_url": "optional legacy async reply URL"
  }

Response: {"ok": true, "status": "...", "reply": {"text": "..."}}

Env (all optional until OPENCLAW_WECHAT_ENABLED=1):
  OPENCLAW_WECHAT_ENABLED   master switch, default off
  OPENCLAW_WEBHOOK_TOKEN    required when enabled; Bearer the plugin must send
  OPENCLAW_GATEWAY_URL      e.g. http://127.0.0.1:18789 (for async push)
  OPENCLAW_GATEWAY_TOKEN    Gateway auth token for /tools/invoke
  OPENCLAW_WECHAT_CHANNEL   default openclaw-weixin
  OPENCLAW_SYNC_WAIT_SECONDS default 12 (then async push if gateway configured)
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Optional

import httpx

from app.channels.base import ChannelAdapter, ChannelInbound, ChannelReply, normalize_text_event
from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id

CHANNEL = "openclaw_wechat"


def webhook_token() -> str:
    return (os.getenv("OPENCLAW_WEBHOOK_TOKEN") or "").strip()


def gateway_url() -> str:
    return (os.getenv("OPENCLAW_GATEWAY_URL") or "").strip().rstrip("/")


def gateway_token() -> str:
    return (os.getenv("OPENCLAW_GATEWAY_TOKEN") or "").strip()


def wechat_channel_id() -> str:
    return (os.getenv("OPENCLAW_WECHAT_CHANNEL") or "openclaw-weixin").strip() or "openclaw-weixin"


def sync_wait_seconds() -> float:
    return float(os.getenv("OPENCLAW_SYNC_WAIT_SECONDS") or "12")


def can_push() -> bool:
    return bool(gateway_url() and gateway_token())


def verify_inbound_auth(headers: Optional[dict[str, str]] = None) -> bool:
    """Bearer / x-openclaw-token must match. No token configured → reject."""
    expected = webhook_token()
    if not expected:
        return False
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    auth = headers.get("authorization") or ""
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token = token or headers.get("x-openclaw-token") or ""
    return hmac.compare_digest(token.encode(), expected.encode())


def parse_openclaw_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenClaw bridge payloads to the shared inbound dict."""
    root = payload.get("openclaw") if isinstance(payload.get("openclaw"), dict) else payload
    data = root.get("data") if isinstance(root.get("data"), dict) else root

    external = (
        data.get("from")
        or data.get("sender_id")
        or data.get("user_id")
        or data.get("sender")
        or data.get("wxid")
        or data.get("external_user_id")
        or data.get("peer")
        or payload.get("from")
        or "unknown"
    )
    text = (
        data.get("text")
        or data.get("content")
        or data.get("body")
        or data.get("message")
        or payload.get("text")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("text") or text.get("content") or str(text)

    message_id = str(
        data.get("msg_id") or data.get("message_id") or data.get("id") or payload.get("id") or ""
    )
    chat_id = str(
        data.get("conversation_id") or data.get("chat_id") or data.get("room_id") or data.get("to") or ""
    )

    chat_type = str(data.get("chat_type") or data.get("type") or "").lower()
    is_group = bool(
        data.get("room_id")
        or data.get("is_group")
        or data.get("isGroup")
        or chat_type in ("group", "room", "chatroom")
    )

    value = data.get("value") if isinstance(data.get("value"), dict) else {}
    thread_id = str(data.get("thread_id") or value.get("thread_id") or "")
    if (data.get("confirmed") is True or value.get("confirmed") is True) and not text:
        text = "CONFIRM"
    elif (data.get("confirmed") is False or value.get("confirmed") is False) and not text:
        text = "REJECT"

    callback_url = str(data.get("callback_url") or data.get("reply_to") or payload.get("callback_url") or "")

    inbound = _to_inbound(
        str(external),
        str(text or ""),
        message_id=message_id,
        chat_id=chat_id,
        is_group=is_group,
        thread_id=thread_id,
        raw=payload,
    )
    out = _as_dict(inbound)
    out["account_id"] = str(data.get("account_id") or data.get("accountId") or "")
    out["display_name"] = str(data.get("sender_name") or data.get("senderName") or "")
    if value:
        out["action_value"] = dict(value)
    return {
        "type": "inbound",
        "inbound": out,
        "kind": data.get("event") or data.get("type") or "message",
        "gateway": "openclaw",
        "callback_url": callback_url,
    }


def render_wechat_text(reply: ChannelReply) -> str:
    """WeChat has no interactive cards: options → numbered list, links → bare URLs."""
    parts: list[str] = []
    if reply.title:
        parts.append(f"【{reply.title}】")
    if reply.text:
        parts.append(reply.text)
    if reply.options:
        lines = [f"{i}. {opt.label}" for i, opt in enumerate(reply.options, start=1)]
        parts.append("回复数字选择：\n" + "\n".join(lines))
    for link in reply.links:
        parts.append(f"{link.label}：{link.url}")
    return "\n\n".join(parts).strip()


def send_via_gateway(*, to: str, text: str, account_id: str = "") -> dict[str, Any]:
    """Gateway ``POST /tools/invoke`` → ``message`` tool, ``action=send``."""
    if not can_push():
        return {"ok": False, "skipped": True, "reason": "no_OPENCLAW_GATEWAY_URL_or_TOKEN"}
    args: dict[str, Any] = {"channel": wechat_channel_id(), "target": to, "message": text}
    if account_id:
        args["accountId"] = account_id
    headers = {
        "Authorization": f"Bearer {gateway_token()}",
        "Content-Type": "application/json",
        "x-openclaw-message-channel": wechat_channel_id(),
    }
    if account_id:
        headers["x-openclaw-account-id"] = account_id
    body = {"tool": "message", "action": "send", "args": args, "sessionKey": "main"}
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(f"{gateway_url()}/tools/invoke", headers=headers, json=body)
        data = r.json() if r.content else {}
    except Exception as exc:
        return {"ok": False, "via": "openclaw_tools_invoke", "error": type(exc).__name__}
    ok = r.status_code < 400 and bool((data or {}).get("ok", True))
    return {"ok": ok, "via": "openclaw_tools_invoke", "status_code": r.status_code}


def send_via_callback(*, callback_url: str, to: str, text: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(callback_url, json={"text": text, "to": to, "channel": wechat_channel_id()})
        return {"ok": r.status_code < 400, "via": "callback_url", "status_code": r.status_code}
    except Exception as exc:
        return {"ok": False, "via": "callback_url", "error": type(exc).__name__}


def deliver_outbound(
    *,
    text: str,
    external_user_id: str = "",
    card: Optional[dict[str, Any]] = None,
    callback_url: str = "",
    account_id: str = "",
    channel_hint: str = "",
) -> dict[str, Any]:
    body_text = text or ""
    if card and not body_text:
        body_text = str(card.get("preview") or card.get("note") or "待确认草稿")
    if callback_url:
        return send_via_callback(callback_url=callback_url, to=external_user_id, text=body_text)
    if not external_user_id:
        return {"ok": False, "reason": "no_target"}
    return send_via_gateway(to=external_user_id, text=body_text, account_id=account_id)


def config_status() -> dict[str, Any]:
    from app.channels.settings import wechat_enabled

    return {
        "switch": wechat_enabled(),
        "has_webhook_token": bool(webhook_token()),
        "gateway_url_set": bool(gateway_url()),
        "has_gateway_token": bool(gateway_token()),
        "can_push": can_push(),
        "wechat_channel": wechat_channel_id(),
        "sync_wait_seconds": sync_wait_seconds(),
        "contract": (
            "OpenClaw before_dispatch bridge → POST /webhooks/openclaw/wechat; "
            "push → OPENCLAW_GATEWAY_URL/tools/invoke (message.send)"
        ),
        "wechat_transport": "OpenClaw openclaw-weixin plugin (Tencent iLink long-poll), not WeChat OA webhook",
    }


class OpenClawWeChatAdapter(ChannelAdapter):
    name = CHANNEL
    display_name = "微信"
    aliases = ("openclaw-weixin", "wechat", "weixin")
    supports_cards = False

    def enabled(self) -> bool:
        from app.channels.settings import wechat_enabled

        return wechat_enabled() and bool(webhook_token())

    def config_status(self) -> dict[str, Any]:
        return config_status()

    def render(self, reply: ChannelReply) -> dict[str, Any]:
        return {"text": render_wechat_text(reply), "card": None}

    def send(
        self,
        *,
        external_user_id: str,
        rendered: dict[str, Any],
        message_id: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "wechat_disabled"}
        ctx = context or {}
        return deliver_outbound(
            text=str(rendered.get("text") or ""),
            external_user_id=external_user_id,
            callback_url=str(ctx.get("callback_url") or ""),
            account_id=str(ctx.get("account_id") or ""),
        )


def _to_inbound(
    external_user_id: str,
    text: str,
    *,
    message_id: str = "",
    chat_id: str = "",
    is_group: bool = False,
    thread_id: str = "",
    raw: dict[str, Any] | None = None,
) -> ChannelInbound:
    uid = resolve_user_id(CHANNEL, external_user_id)
    inbound = normalize_text_event(
        channel=CHANNEL,
        external_user_id=external_user_id,
        text=text,
        message_id=message_id,
        chat_id=chat_id,
        raw=raw,
        user_id=uid,
    )
    inbound.raw = dict(inbound.raw or {})
    inbound.raw["_is_group"] = bool(is_group)
    if thread_id:
        inbound.raw["_thread_id"] = thread_id
    return inbound


def _as_dict(inbound: ChannelInbound) -> dict[str, Any]:
    raw = dict(inbound.raw or {})
    return {
        "channel": inbound.channel,
        "external_user_id": inbound.external_user_id,
        "text": inbound.text,
        "message_id": inbound.message_id,
        "chat_id": inbound.chat_id,
        "user_id": inbound.user_id,
        "received_at": inbound.received_at,
        "bound": bool(inbound.user_id),
        "is_group": bool(raw.get("_is_group")),
        "thread_id": str(raw.get("_thread_id") or ""),
        "raw": raw,
    }
