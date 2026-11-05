"""User-visible memory card: profile facts + key episodic items.

Reads and writes the same SQLite rows ``format_for_prompt`` uses, so an
edit is visible on the next prompt build. Every call is scoped to one user.
A thread id, when present, must belong to that user.
"""

from __future__ import annotations

from app.agent.tool_registry import tool

from functools import wraps
from typing import Any, Optional

from app import db

from app.agent.memory import episodic, profile as profile_mem
from app.agent.memory.profile import LABELS, LIST_FIELDS as _LIST_FIELDS, SCALAR_FIELDS as _SCALAR_FIELDS
from app.agent.memory.redact import display_text, is_secret_key, redact_text

SCALAR_FIELDS = _SCALAR_FIELDS + ("dialogue_summary",)
LIST_FIELDS = _LIST_FIELDS

KIND_LABELS = {
    "profile_updated": "档案更新",
    "draft_created": "生成草稿",
    "confirm_accepted": "确认草稿",
    "confirm_rejected": "不用草稿",
    "chat": "对话",
    "summary": "对话要点",
    "proactive": "主动提醒",
    "reminder": "提醒",
    "schedule": "定时任务",
    "secret_saved": "安全保存",
    "secret_deleted": "删除保存项",
    "privacy_redacted": "隐私拦截",
    "workout": "训练记录",
    "sport_plan": "训练计划",
}


class MemoryAccessError(Exception):
    def __init__(self, http_status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


def _uid(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid:
        raise MemoryAccessError(400, "user_required", "需要当前用户")
    return uid


def assert_scope(user_id: str, thread_id: Optional[str] = None) -> str:
    """Reject a thread that already belongs to someone else."""
    uid = _uid(user_id)
    tid = str(thread_id or "").strip()
    if not tid:
        return uid
    from app.graph import get_thread_snapshot

    snap = get_thread_snapshot(tid)
    if snap.get("exists"):
        owner = str(snap.get("user_id") or "").strip()
        if owner and owner != uid:
            raise MemoryAccessError(403, "thread_forbidden", "thread_id 不属于当前用户")
    return uid


def _parse_fact_id(fact_id: str) -> tuple[str, Optional[int]]:
    raw = str(fact_id or "").strip()
    if raw in SCALAR_FIELDS:
        return raw, None
    if ":" in raw:
        field, _, idx = raw.partition(":")
        if field in LIST_FIELDS and idx.isdigit():
            return field, int(idx)
    if raw in LIST_FIELDS:
        raise MemoryAccessError(400, "fact_index_required", "请指定要修改的那一条")
    raise MemoryAccessError(400, "bad_fact", "没有这项记忆")


def _status(code: str) -> MemoryAccessError:
    if code == "forbidden":
        return MemoryAccessError(403, "forbidden", "这条记忆不属于当前用户")
    return MemoryAccessError(404, "not_found", "找不到这条记忆")


def _assert_event_scope(user_id: str, event: dict[str, Any], thread_id: Optional[str]) -> None:
    if str(event.get("user_id") or "") != user_id:
        raise MemoryAccessError(403, "forbidden", "这条记忆不属于当前用户")
    tid = str(thread_id or "").strip()
    owner_thread = str(event.get("thread_id") or "").strip()
    if tid and owner_thread and owner_thread != tid:
        raise MemoryAccessError(403, "thread_forbidden", "这条记忆不属于当前会话")


@tool('memory_card', risk_level='read', description='card view')
def card_view(user_id: str, *, thread_id: Optional[str] = None, limit: int = 8) -> dict[str, Any]:
    """Redacted facts + recent episodic lines for the in-thread card."""
    uid = assert_scope(user_id, thread_id)
    limit = max(1, min(int(limit or 8), 20))
    stored = profile_mem.get(uid) or {}
    facts: list[dict[str, Any]] = []
    for field in SCALAR_FIELDS:
        if is_secret_key(field):
            continue
        text = display_text(str(stored.get(field) or ""))
        if not text:
            continue
        facts.append(
            {
                "id": field,
                "field": field,
                "label": LABELS[field],
                "value": text[:2000],
                "kind": "profile",
            }
        )
    for field in LIST_FIELDS:
        if is_secret_key(field):
            continue
        for index, item in enumerate(list(stored.get(field) or [])):
            # Keep original indices so editing targets the stored row.
            text = display_text(str(item))
            if not text:
                continue
            facts.append(
                {
                    "id": f"{field}:{index}",
                    "field": field,
                    "index": index,
                    "label": LABELS[field],
                    "value": text[:500],
                    "kind": "profile",
                }
            )

    episodes: list[dict[str, Any]] = []
    for ev in episodic.search_events(uid, limit=40):
        if is_secret_key(str(ev.get("kind") or "")):
            continue
        text = display_text(str(ev.get("summary") or ""))
        if not text:
            continue
        kind = str(ev.get("kind") or "")
        episodes.append(
            {
                "id": ev.get("id"),
                "kind": kind,
                "label": KIND_LABELS.get(kind, "记忆"),
                "value": text[:2000],
                "thread_id": ev.get("thread_id") or "",
                "created_at": ev.get("created_at"),
            }
        )
        if len(episodes) >= limit:
            break
    return {
        "user_id": uid,
        "thread_id": str(thread_id or ""),
        "facts": facts,
        "episodes": episodes,
    }


def _drop_mentions(summary: str, word: str) -> str:
    """Remove summary sentences that mention a value the user has replaced."""
    import re as _re

    parts = _re.split(r"(?<=[。；;！!？?\n])", summary or "")
    return "".join(p for p in parts if word not in p).strip()


def _revise_memory(fn):
    @wraps(fn)
    def wrapped(user_id, *args, **kwargs):
        uid = _uid(user_id)
        with db.memory_transaction():
            revision = int((profile_mem.get(uid) or {}).get("memory_rev", 0))
            result = fn(uid, *args, **kwargs)
            # Keep the counter even when forget_all clears the profile.
            profile_mem.merge(uid, {"memory_rev": revision + 1})
            return result

    return wrapped


@tool('memory_fact_update', risk_level='self_write', description='update fact')
@_revise_memory
def update_fact(
    user_id: str,
    fact_id: str,
    value: str,
    *,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    uid = assert_scope(user_id, thread_id)
    field, index = _parse_fact_id(fact_id)
    if is_secret_key(field):
        raise MemoryAccessError(403, "forbidden", "这项内容不能显示或修改")
    text = redact_text(str(value or "")).strip()
    if not text:
        raise MemoryAccessError(400, "empty", "内容不能为空")
    stored = dict(profile_mem.get(uid) or {})
    if index is None:
        old = str(stored.get(field) or "").strip()
        stored[field] = text[:2000]
        if field in _SCALAR_FIELDS:
            stored["pinned_fields"] = sorted(set(stored.get("pinned_fields") or []) | {field})
        if field in _SCALAR_FIELDS and old and old != stored[field]:
            stored["dialogue_summary"] = _drop_mentions(str(stored.get("dialogue_summary") or ""), old)
        if field == "city" and old and old != stored[field]:
            prev = [c for c in (stored.get("previous_cities") or []) if c != stored[field]]
            stored["previous_cities"] = ([old] + [c for c in prev if c != old])[:5]
    else:
        items = [str(x) for x in list(stored.get(field) or [])]
        if index >= len(items):
            raise MemoryAccessError(404, "not_found", "找不到这条记忆")
        items[index] = text[:500]
        stored[field] = items
    profile_mem.save(uid, stored)
    return card_view(uid, thread_id=thread_id)


@tool('memory_fact_delete', risk_level='self_write', description='delete fact')
@_revise_memory
def delete_fact(
    user_id: str,
    fact_id: str,
    *,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    uid = assert_scope(user_id, thread_id)
    field, index = _parse_fact_id(fact_id)
    stored = dict(profile_mem.get(uid) or {})
    if index is None:
        stored.pop(field, None)
    else:
        items = [str(x) for x in list(stored.get(field) or [])]
        if index >= len(items):
            raise MemoryAccessError(404, "not_found", "找不到这条记忆")
        del items[index]
        stored[field] = items
    profile_mem.save(uid, stored)
    return card_view(uid, thread_id=thread_id)


@tool('memory_episode_update', risk_level='self_write', description='update episode')
@_revise_memory
def update_episode(
    user_id: str,
    event_id: int,
    value: str,
    *,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    uid = assert_scope(user_id, thread_id)
    event = episodic.get_event(int(event_id))
    if not event:
        raise MemoryAccessError(404, "not_found", "找不到这条记忆")
    _assert_event_scope(uid, event, thread_id)
    text = redact_text(str(value or "")).strip()
    if not text:
        raise MemoryAccessError(400, "empty", "内容不能为空")
    status = episodic.update_event_summary(int(event_id), uid, text[:2000])
    if status != "ok":
        raise _status(status)
    return card_view(uid, thread_id=thread_id)


@tool('memory_episode_delete', risk_level='self_write', description='delete episode')
@_revise_memory
def delete_episode(
    user_id: str,
    event_id: int,
    *,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    uid = assert_scope(user_id, thread_id)
    event = episodic.get_event(int(event_id))
    if not event:
        raise MemoryAccessError(404, "not_found", "找不到这条记忆")
    _assert_event_scope(uid, event, thread_id)
    status = episodic.delete_event(int(event_id), uid)
    if status != "ok":
        raise _status(status)
    return card_view(uid, thread_id=thread_id)


@tool('memory_forget', risk_level='self_write', description='forget all')
@_revise_memory
def forget_all(
    user_id: str,
    *,
    confirm: bool,
    thread_id: Optional[str] = None,
) -> dict[str, Any]:
    uid = assert_scope(user_id, thread_id)
    if not confirm:
        raise MemoryAccessError(400, "confirm_required", "需要确认后才能忘掉全部记忆")
    profile_mem.save(uid, {})
    episodic.delete_user_events(uid)
    return card_view(uid, thread_id=thread_id)
