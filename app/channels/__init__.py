"""Multi-channel layer: adapters (Feishu / WeChat via OpenClaw / reserved QQ) → shared gateway."""

from app.channels.base import (
    ChannelAdapter,
    ChannelInbound,
    ChannelLink,
    ChannelOption,
    ChannelOutbound,
    ChannelReply,
    normalize_text_event,
)
from app.channels.binding import (
    apply_auto_bindings,
    bind_channel,
    get_binding,
    list_bindings,
    resolve_user_id,
    unbind_channel,
)
from app.channels.feishu import parse_feishu_webhook
from app.channels import feishu_ws
from app.channels.openclaw_wechat import parse_openclaw_webhook
from app.channels.outbound import build_confirm_outbound, deliver_channel_outbound, outbound_as_dict
from app.channels.registry import channels_status, get_adapter, list_adapters, register_adapter

__all__ = [
    "ChannelAdapter",
    "ChannelInbound",
    "ChannelLink",
    "ChannelOption",
    "ChannelOutbound",
    "ChannelReply",
    "apply_auto_bindings",
    "bind_channel",
    "build_confirm_outbound",
    "channels_status",
    "deliver_channel_outbound",
    "feishu_ws",
    "get_adapter",
    "get_binding",
    "list_adapters",
    "list_bindings",
    "normalize_text_event",
    "outbound_as_dict",
    "parse_feishu_webhook",
    "parse_openclaw_webhook",
    "register_adapter",
    "resolve_user_id",
    "unbind_channel",
]
