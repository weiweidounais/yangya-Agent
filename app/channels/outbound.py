"""Channel-neutral reply builders + delivery through the adapter registry."""

from __future__ import annotations

from typing import Any, Optional
from contextlib import nullcontext
from functools import wraps

from app.channels.base import ChannelLink, ChannelOption, ChannelOutbound, ChannelReply
from app.channels.privacy import redact

KIND_LABELS = {"email": "邮件", "message": "消息", "article": "文章", "note": "笔记", "other": "草稿"}


def guard_rendered(rendered: dict[str, Any], *, user_id: str, inputs: tuple[str, ...] = ()) -> dict[str, Any]:
    """Apply the last-mile leak guard to text and all nested card strings."""
    from app.agent.harness.guardrails import guard_outbound_text

    def guard(value: Any) -> Any:
        if isinstance(value, str):
            return guard_outbound_text(value, user_id=user_id, inputs=inputs)
        if isinstance(value, dict):
            return {key: guard(item) for key, item in value.items()}
        if isinstance(value, list):
            return [guard(item) for item in value]
        return value

    return guard(rendered)


def confirm_options(thread_id: str) -> list[ChannelOption]:
    return [
        ChannelOption(
            id="confirm",
            label="确认",
            style="primary",
            value={"command": "CONFIRM", "confirmed": True, "thread_id": thread_id},
        ),
        ChannelOption(
            id="reject",
            label="拒绝",
            style="danger",
            value={"command": "REJECT", "confirmed": False, "thread_id": thread_id},
        ),
    ]


def build_confirm_reply(
    *,
    title: str = "",
    body: str,
    kind: str = "other",
    thread_id: str,
    draft_url: str = "",
) -> ChannelReply:
    """Confirm card: short preview only; the full draft stays behind the web link."""
    preview = redact((body or "")[:400]) or "(空草稿)"
    label = KIND_LABELS.get(kind, "草稿")
    lines = []
    if title:
        lines.append(f"标题：{redact(title[:120])}")
    lines.append(f"{label}预览：\n{preview}")
    if len(body or "") > 400:
        lines.append("（以上为节选，完整内容见网页链接）")
    lines.append("确认后才算定稿；后点的选择会覆盖先点的。回复「拒绝：原因」可以让我重写。")
    links = [ChannelLink(label="在网页查看完整草稿", url=draft_url)] if draft_url else []
    return ChannelReply(
        text="\n".join(lines),
        title=f"待确认：{label}草稿",
        kind="confirm",
        options=confirm_options(thread_id),
        links=links,
        needs_confirm=True,
        thread_id=thread_id,
    )


def build_confirm_outbound(
    *,
    channel: str,
    external_user_id: str,
    body: str,
    thread_id: str,
    title: str = "",
    user_id: Optional[str] = None,
) -> ChannelOutbound:
    """Legacy helper kept for callers that want a rendered ChannelOutbound."""
    from app.channels.registry import get_adapter

    reply = build_confirm_reply(title=title, body=body, thread_id=thread_id)
    adapter = get_adapter(channel)
    rendered = adapter.render(reply) if adapter else {"text": reply.text, "card": None}
    return ChannelOutbound(
        channel=channel,
        external_user_id=external_user_id,
        text=rendered["text"],
        needs_confirm=True,
        card=rendered.get("card"),
        user_id=user_id,
    )


def outbound_as_dict(out: ChannelOutbound) -> dict[str, Any]:
    return {
        "channel": out.channel,
        "external_user_id": out.external_user_id,
        "text": out.text,
        "needs_confirm": out.needs_confirm,
        "card": out.card,
        "user_id": out.user_id,
    }


def _bound_identity(fn):
    @wraps(fn)
    def scoped(*args, **kwargs):
        from app.data_access import user_scope
        channel = kwargs.get("channel") if "channel" in kwargs else args[0]
        external = kwargs.get("external_user_id") if "external_user_id" in kwargs else args[1]
        uid = _bound_user_id(channel, external)
        with user_scope(uid) if uid else nullcontext():
            return fn(*args, **kwargs)
    return scoped


@_bound_identity
def send_reply(
    channel: str,
    external_user_id: str,
    reply: ChannelReply,
    *,
    message_id: str = "",
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from app.channels.registry import get_adapter

    adapter = get_adapter(channel)
    if adapter is None:
        return {"ok": False, "skipped": True, "reason": f"no_adapter_for_{channel}"}
    try:
        rendered = adapter.render(reply)
    except NotImplementedError:
        return {"ok": False, "skipped": True, "reason": f"{channel}_not_implemented"}
    return adapter.send(
        external_user_id=external_user_id,
        rendered=guard_rendered(rendered, user_id=_bound_user_id(channel, external_user_id)),
        message_id=message_id,
        context=context,
    )


@_bound_identity
def deliver_channel_outbound(
    *,
    channel: str,
    external_user_id: str,
    text: str,
    card: dict[str, Any] | None = None,
    message_id: str = "",
    callback_url: str = "",
    account_id: str = "",
) -> dict[str, Any]:
    """Push already-rendered text/card. Disabled channels are skipped."""
    from app.channels.registry import get_adapter

    adapter = get_adapter(channel)
    if adapter is None:
        return {"ok": False, "skipped": True, "reason": f"no_deliver_for_{channel}"}
    return adapter.send(
        external_user_id=external_user_id,
        rendered=guard_rendered(
            {"text": redact(text or ""), "card": card},
            user_id=_bound_user_id(channel, external_user_id),
        ),
        message_id=message_id,
        context={"callback_url": callback_url, "account_id": account_id},
    )


def _bound_user_id(channel: str, external_user_id: str) -> str:
    from app.data_access.channel_identity import resolve_channel_identity as resolve_user_id

    return str(resolve_user_id(channel, external_user_id) or "")
