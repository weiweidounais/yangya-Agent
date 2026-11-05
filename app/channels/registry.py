"""Channel adapter registry. New channels: subclass ChannelAdapter + register_adapter()."""

from __future__ import annotations

from typing import Any, Optional

from app.channels.base import ChannelAdapter

_ADAPTERS: dict[str, ChannelAdapter] = {}
_ALIASES: dict[str, str] = {}


def register_adapter(adapter: ChannelAdapter) -> ChannelAdapter:
    if not adapter.name:
        raise ValueError("adapter.name required")
    _ADAPTERS[adapter.name] = adapter
    for alias in adapter.aliases:
        _ALIASES[alias] = adapter.name
    return adapter


def canonical_name(channel: str) -> str:
    c = (channel or "").strip()
    return _ALIASES.get(c, c)


def get_adapter(channel: str) -> Optional[ChannelAdapter]:
    _load_builtin()
    return _ADAPTERS.get(canonical_name(channel))


def list_adapters() -> list[ChannelAdapter]:
    _load_builtin()
    return list(_ADAPTERS.values())


def channels_status() -> dict[str, Any]:
    return {
        a.name: {
            "display_name": a.display_name,
            "enabled": a.enabled(),
            "reserved": a.reserved,
            "supports_cards": a.supports_cards,
            **a.config_status(),
        }
        for a in list_adapters()
    }


_loaded = False


def _load_builtin() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.channels.feishu import FeishuAdapter
    from app.channels.openclaw_wechat import OpenClawWeChatAdapter
    from app.channels.qq import QQAdapter

    for cls in (FeishuAdapter, OpenClawWeChatAdapter, QQAdapter):
        if cls.name not in _ADAPTERS:
            register_adapter(cls())
