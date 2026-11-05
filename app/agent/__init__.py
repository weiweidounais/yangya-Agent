"""Personal Agent core: router, tool registry, subgraphs, memory, loop, harness."""

from __future__ import annotations

from app.agent import tool_registry


def __getattr__(name):
    # Keep public exports lazy so low-level tool modules can import the registry.
    if name in {"format_memory_context", "search_events", "write_event"}:
        from app.agent import memory
        return getattr(memory, name)
    if name in {"classify_text", "describe_route", "route_intent"}:
        from app.agent import router
        return getattr(router, name)
    raise AttributeError(name)


__all__ = [
    "classify_text",
    "describe_route",
    "format_memory_context",
    "route_intent",
    "search_events",
    "tool_registry",
    "write_event",
]
