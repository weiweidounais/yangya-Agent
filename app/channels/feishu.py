"""Feishu bot webhook — normalize inbound + verify challenge/token/signature."""

from __future__ import annotations

from typing import Any, Optional

from app.channels.base import ChannelAdapter, ChannelInbound, ChannelReply, normalize_text_event
from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id
from app.channels import feishu_client

CHANNEL = "feishu"

_BUTTON_TYPES = {"primary": "primary", "danger": "danger"}


def render_feishu_card(reply: ChannelReply) -> dict[str, Any]:
    """ChannelReply → Feishu interactive card (message card JSON 1.0).

    Option buttons carry ``value`` → delivered back via ``card.action.trigger``
    over the long connection. Links become URL buttons.
    """
    template = {"confirm": "orange", "options": "orange", "bind": "blue", "privacy": "red"}.get(
        reply.kind, "blue"
    )
    elements: list[dict[str, Any]] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": reply.text or " "}}
    ]
    if reply.options:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": opt.label},
                        "type": _BUTTON_TYPES.get(opt.style, "default"),
                        "value": {**opt.value, "opt": opt.id},
                    }
                    for opt in reply.options
                ],
            }
        )
    if reply.links:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": link.label},
                        "type": "default",
                        "url": link.url,
                    }
                    for link in reply.links
                ],
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": reply.title or "个人助理"},
        },
        "elements": elements,
    }


class FeishuAdapter(ChannelAdapter):
    name = CHANNEL
    display_name = "飞书"
    aliases = ("lark",)
    supports_cards = True

    def enabled(self) -> bool:
        from app.channels.settings import feishu_enabled

        return feishu_enabled() and feishu_client.is_configured()

    def config_status(self) -> dict[str, Any]:
        from app.channels.settings import feishu_enabled

        return {"switch": feishu_enabled(), **feishu_client.config_status()}

    def render(self, reply: ChannelReply) -> dict[str, Any]:
        if reply.options or reply.links or reply.title:
            return {"text": reply.text, "card": render_feishu_card(reply)}
        return {"text": reply.text, "card": None}

    def send(
        self,
        *,
        external_user_id: str,
        rendered: dict[str, Any],
        message_id: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "feishu_disabled"}
        return feishu_client.deliver_outbound(
            external_user_id=external_user_id,
            text=str(rendered.get("text") or ""),
            card=rendered.get("card"),
            message_id=message_id,
        )


def parse_feishu_webhook(
    payload: dict[str, Any],
    *,
    raw_body: bytes | str | None = None,
    headers: Optional[dict[str, str]] = None,
    skip_auth: bool = False,
) -> dict[str, Any]:
    """Handle Feishu URL challenge, encrypted body, message events, card actions.

    Returns ``{"type": "challenge", ...}``, ``{"type": "error", ...}``,
    or ``{"type": "inbound", "inbound": ...}``.

    ``skip_auth=True`` for long-connection (WS) events: SDK already authenticated
    the channel; no HTTP signature / verification-token check.
    """
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}

    # Optional signature check when encrypt key configured (HTTP webhook only)
    if not skip_auth and feishu_client.encrypt_key() and raw_body is not None:
        ok = feishu_client.verify_signature(
            timestamp=headers.get("x-lark-request-timestamp", ""),
            nonce=headers.get("x-lark-request-nonce", ""),
            body=raw_body,
            signature=headers.get("x-lark-signature", ""),
        )
        if not ok:
            return {"type": "error", "error": "invalid_signature", "status_code": 401}

    try:
        payload = feishu_client.maybe_unwrap_payload(payload if isinstance(payload, dict) else {})
    except Exception as exc:
        return {
            "type": "error",
            "error": "decrypt_failed",
            "detail": f"{type(exc).__name__}",
            "status_code": 400,
        }

    # URL verification
    if payload.get("type") == "url_verification" or (
        "challenge" in payload and not payload.get("event") and not payload.get("action")
    ):
        if not skip_auth and not feishu_client.verify_url_token(payload):
            return {"type": "error", "error": "invalid_verification_token", "status_code": 401}
        return {"type": "challenge", "challenge": payload.get("challenge", "")}

    # Event token check (when configured; skipped for WS long connection)
    if not skip_auth and not feishu_client.verify_url_token(payload):
        return {"type": "error", "error": "invalid_verification_token", "status_code": 401}

    header = payload.get("header") or {}
    event = payload.get("event") or payload

    # Card action callback (confirm / reject)
    if payload.get("action") or (event.get("action") if isinstance(event, dict) else None):
        action = payload.get("action") or event.get("action") or {}
        value = action.get("value") if isinstance(action, dict) else {}
        if not isinstance(value, dict):
            value = {"raw": value}
        operator = (
            payload.get("operator")
            or event.get("operator")
            or (event.get("sender") if isinstance(event, dict) else {})
            or {}
        )
        open_id = (
            operator.get("open_id")
            or operator.get("user_id")
            or payload.get("open_id")
            or "unknown"
        )
        text = value.get("command") or value.get("text") or ""
        if value.get("confirmed") is True:
            text = text or "CONFIRM"
        elif value.get("confirmed") is False:
            text = text or "REJECT"
        thread_id = str(value.get("thread_id") or "")
        inbound = _to_inbound(
            open_id,
            text,
            message_id=str(payload.get("open_message_id") or ""),
            chat_id=str(payload.get("open_chat_id") or ""),
            is_group=False,
            thread_id=thread_id,
            raw=payload,
        )
        out = _as_dict(inbound)
        out["action_value"] = dict(value)
        return {"type": "inbound", "inbound": out, "kind": "card_action"}

    # IM message
    message = event.get("message") if isinstance(event, dict) else None
    sender = event.get("sender") if isinstance(event, dict) else None
    if not isinstance(message, dict):
        message = payload.get("message") or {}
    if not isinstance(sender, dict):
        sender = payload.get("sender") or {}

    open_id = (
        (sender.get("sender_id") or {}).get("open_id")
        if isinstance(sender.get("sender_id"), dict)
        else None
    ) or sender.get("open_id") or sender.get("user_id") or payload.get("open_id") or "unknown"

    content = message.get("content") or payload.get("text") or ""
    text = content
    if isinstance(content, str) and content.startswith("{"):
        import json

        try:
            text = json.loads(content).get("text", content)
        except Exception:
            text = content
    elif isinstance(content, dict):
        text = content.get("text") or str(content)

    chat_type = str(message.get("chat_type") or payload.get("chat_type") or "").lower()
    is_group = chat_type in ("group", "public", "topic")
    if chat_type == "p2p":
        is_group = False
    elif chat_type == "group":
        is_group = True

    inbound = _to_inbound(
        str(open_id),
        str(text or ""),
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        is_group=is_group,
        raw=payload,
    )
    return {"type": "inbound", "inbound": _as_dict(inbound), "kind": "message"}


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
    if isinstance(inbound.raw, dict):
        inbound.raw = dict(inbound.raw)
        inbound.raw["_is_group"] = bool(is_group)
        if thread_id:
            inbound.raw["_thread_id"] = thread_id
    return inbound


def _as_dict(inbound: ChannelInbound) -> dict[str, Any]:
    raw = dict(inbound.raw or {})
    is_group = bool(raw.get("_is_group"))
    thread_id = str(raw.get("_thread_id") or "")
    return {
        "channel": inbound.channel,
        "external_user_id": inbound.external_user_id,
        "text": inbound.text,
        "message_id": inbound.message_id,
        "chat_id": inbound.chat_id,
        "user_id": inbound.user_id,
        "received_at": inbound.received_at,
        "bound": bool(inbound.user_id),
        "is_group": is_group,
        "thread_id": thread_id,
        "raw": raw,
    }
