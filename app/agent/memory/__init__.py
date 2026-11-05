"""Three-layer memory (no vectors): working / profile / episodic."""

from __future__ import annotations

from app.agent.tool_registry import tool

from typing import Any, Optional

from app.agent.memory import episodic, profile, summary

__all__ = [
    "episodic",
    "profile",
    "summary",
    "write_event",
    "search_events",
    "format_memory_context",
    "get_profile",
    "merge_profile",
    "maybe_summarize_turn",
]


def write_event(
    user_id: str,
    kind: str,
    summary: str,
    *,
    tags: Optional[list[str]] = None,
    payload: Optional[dict[str, Any]] = None,
    thread_id: str = "",
) -> dict[str, Any]:
    return episodic.write_event(
        user_id, kind, summary, tags=tags, payload=payload, thread_id=thread_id
    )


def search_events(
    user_id: str,
    *,
    query: str = "",
    tags: Optional[list[str]] = None,
    kind: Optional[str] = None,
    thread_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return episodic.search_events(
        user_id, query=query, tags=tags, kind=kind, thread_id=thread_id, limit=limit
    )


@tool('memory_context', risk_level='read', description='Format profile + episodic context for prompts',
      tags=['memory'], sensitive=False)
def format_memory_context(
    user_id: str,
    *,
    query: str = "",
    tags: Optional[list[str]] = None,
    limit: int = 8,
    include_profile: bool = True,
) -> str:
    from app.agent.context_assembler import memory_context

    return memory_context(user_id, query=query, tags=tags, limit=limit,
                          include_profile=include_profile)


def get_profile(user_id: str) -> dict[str, Any]:
    return profile.get(user_id)


def merge_profile(user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return profile.merge(user_id, patch)


def maybe_summarize_turn(*args, **kwargs):
    return summary.maybe_summarize_turn(*args, **kwargs)
