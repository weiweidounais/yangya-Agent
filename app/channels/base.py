"""Channel protocol: every ingress → ChannelInbound; every reply → ChannelReply.

Adapters (Feishu / WeChat via OpenClaw / reserved QQ …) only translate between
their wire format and these shapes. Business logic lives in
``app.channels.gateway`` and is shared by all channels.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class ChannelInbound:
    """Unified inbound message after adapter normalization."""

    channel: str  # feishu | openclaw_wechat | web
    external_user_id: str
    text: str = ""
    message_id: str = ""
    chat_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Filled after binding lookup
    user_id: Optional[str] = None
    received_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class ChannelOutbound:
    """Outbound reply shape (adapters render to Feishu card / WeChat / etc.)."""

    channel: str
    external_user_id: str
    text: str = ""
    needs_confirm: bool = False
    card: Optional[dict[str, Any]] = None
    user_id: Optional[str] = None


@dataclass
class ChannelOption:
    """One selectable choice. ``value`` is echoed back verbatim when picked."""

    id: str
    label: str
    value: dict[str, Any] = field(default_factory=dict)
    style: str = "default"  # primary | danger | default


@dataclass
class ChannelLink:
    label: str
    url: str


@dataclass
class ChannelReply:
    """Channel-neutral reply. Adapters decide card vs. numbered text."""

    text: str
    title: str = ""
    kind: str = "text"  # text | confirm | options | bind | privacy
    options: list[ChannelOption] = field(default_factory=list)
    links: list[ChannelLink] = field(default_factory=list)
    needs_confirm: bool = False
    thread_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ChannelAdapter(ABC):
    """Contract each channel implements. Register via ``registry.register_adapter``."""

    name: str = ""
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    supports_cards: bool = False
    reserved: bool = False

    @abstractmethod
    def enabled(self) -> bool:
        """Master switch (env). Must default to False."""

    @abstractmethod
    def config_status(self) -> dict[str, Any]:
        """Non-secret configuration summary for /health."""

    @abstractmethod
    def render(self, reply: ChannelReply) -> dict[str, Any]:
        """Return ``{"text": str, "card": dict | None}`` for this channel."""

    @abstractmethod
    def send(
        self,
        *,
        external_user_id: str,
        rendered: dict[str, Any],
        message_id: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Push a rendered reply. Never raises; returns ``{"ok": bool, ...}``."""


def normalize_text_event(
    *,
    channel: str,
    external_user_id: str,
    text: str,
    message_id: str = "",
    chat_id: str = "",
    raw: Optional[dict[str, Any]] = None,
    user_id: Optional[str] = None,
) -> ChannelInbound:
    return ChannelInbound(
        channel=channel,
        external_user_id=str(external_user_id or ""),
        text=(text or "").strip(),
        message_id=str(message_id or ""),
        chat_id=str(chat_id or ""),
        raw=dict(raw or {}),
        user_id=user_id,
    )
