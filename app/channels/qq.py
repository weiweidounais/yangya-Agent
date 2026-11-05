"""QQ — reserved extension point, intentionally not implemented.

To add QQ (or any other IM), implement the methods below, give the adapter
an env switch that defaults to OFF, normalize inbound into the dict shape
produced by ``feishu.parse_feishu_webhook`` / ``openclaw_wechat.parse_openclaw_webhook``
and hand it to ``app.channels.gateway.handle_inbound``. Binding, privacy,
options and the agent pipeline then work unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from app.channels.base import ChannelAdapter, ChannelReply


class QQAdapter(ChannelAdapter):
    name = "qq"
    display_name = "QQ"
    aliases = ("qq_bot",)
    supports_cards = False
    reserved = True

    def enabled(self) -> bool:
        return False

    def config_status(self) -> dict[str, Any]:
        return {"implemented": False}

    def render(self, reply: ChannelReply) -> dict[str, Any]:
        raise NotImplementedError("QQ channel is reserved; not implemented")

    def send(
        self,
        *,
        external_user_id: str,
        rendered: dict[str, Any],
        message_id: str = "",
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {"ok": False, "skipped": True, "reason": "qq_not_implemented"}

    def parse_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("QQ channel is reserved; not implemented")
