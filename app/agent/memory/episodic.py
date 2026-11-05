"""Episodic memory: event log + keyword/tag search (no vectors).

Events are written on profile / draft / confirm / reminder changes and
retrieved into chat context.
"""

from __future__ import annotations

from app.agent.tool_registry import tool

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, memory_transaction, init_db
from app.data_access.session import memory_session

# Known event kinds (tags also free-form)
EVENT_KINDS = (
    "profile_updated",
    "draft_created",
    "confirm_accepted",
    "confirm_rejected",
    "chat",
    "summary",
    "proactive",
    "reminder",
    "schedule",
    "secret_saved",
    "secret_deleted",
    "privacy_redacted",
    "workout",
    "sport_plan",
)


class EpisodicEvent(Base):
    __tablename__ = "episodic_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True, default="chat")
    summary: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    thread_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


def _ensure_table() -> None:
    # init_db already joins the active memory transaction for schema creation.
    init_db()


def _row_dict(row: EpisodicEvent) -> dict[str, Any]:
    from app.agent.memory.redact import display_text, is_secret_key, redact_text

    try:
        tags = json.loads(row.tags_json or "[]")
    except json.JSONDecodeError:
        tags = []
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(tags, list):
        tags = []
    if not isinstance(payload, dict):
        payload = {}
    safe_payload: dict[str, Any] = {}
    for key, value in payload.items():
        if is_secret_key(str(key)):
            continue
        if isinstance(value, str):
            text = redact_text(value)
            if text:
                safe_payload[str(key)] = text
        else:
            safe_payload[str(key)] = value
    return {
        "id": row.id,
        "user_id": row.user_id,
        "kind": row.kind,
        "summary": display_text(row.summary or ""),
        "tags": [str(t) for t in tags],
        "payload": safe_payload,
        "thread_id": row.thread_id or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@tool('memory_write', risk_level='self_write', description='Append episodic event',
      tags=['memory'], sensitive=False)
def write_event(
    user_id: str,
    kind: str,
    summary: str,
    *,
    tags: Optional[list[str]] = None,
    payload: Optional[dict[str, Any]] = None,
    thread_id: str = "",
) -> dict[str, Any]:
    """Append an event, or upsert a session summary. Returns {} on failure."""
    try:
        _ensure_table()
        uid = str(user_id or "").strip()
        if not uid:
            return {}
        kind_s = (kind or "chat").strip() or "chat"
        tags_list = [str(t).strip() for t in (tags or []) if str(t).strip()]
        # auto-tag kind
        if kind_s not in tags_list:
            tags_list.insert(0, kind_s)
        from app.agent.harness.leak import memory_safe

        with (memory_transaction() if kind_s == "summary" else memory_session()) as session:
            tid = str(thread_id or "")
            row = None
            if kind_s == "summary":
                rows = list(session.scalars(select(EpisodicEvent).where(
                    EpisodicEvent.user_id == uid,
                    EpisodicEvent.thread_id == tid,
                    EpisodicEvent.kind == "summary",
                ).order_by(EpisodicEvent.id)))
                if rows:
                    row = rows[0]
                    for duplicate in rows[1:]:
                        session.delete(duplicate)
            if row is None:
                row = EpisodicEvent(user_id=uid, kind=kind_s, thread_id=tid)
            row.summary = memory_safe(summary, uid)[:2000]
            row.tags_json = json.dumps(tags_list, ensure_ascii=False)
            payload_text = memory_safe(json.dumps(payload or {}, ensure_ascii=False), uid)
            row.payload_json = payload_text[:8000]
            row.created_at = datetime.now(timezone.utc)
            session.add(row)
            session.flush()
            session.refresh(row)
            return _row_dict(row)
    except Exception:
        return {}


@tool('memory_search', risk_level='read', description='Keyword/tag search episodic memory (no vectors)',
      tags=['memory'], sensitive=False)
def search_events(
    user_id: str,
    *,
    query: str = "",
    tags: Optional[list[str]] = None,
    kind: Optional[str] = None,
    thread_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Keyword / tag search over episodic events (SQLite LIKE, no vectors)."""
    _ensure_table()
    uid = str(user_id or "").strip()
    if not uid:
        return []
    limit = max(1, min(int(limit or 20), 100))
    with memory_session() as session:
        q = select(EpisodicEvent).where(EpisodicEvent.user_id == uid)
        if kind:
            q = q.where(EpisodicEvent.kind == str(kind))
        if thread_id:
            q = q.where(EpisodicEvent.thread_id == str(thread_id))
        q = q.order_by(EpisodicEvent.created_at.desc()).limit(limit * 3)
        rows = list(session.execute(q).scalars().all())

        items = [_row_dict(r) for r in rows]
    qtext = (query or "").strip().lower()
    tag_filters = [str(t).strip().lower() for t in (tags or []) if str(t).strip()]

    def _match(ev: dict[str, Any]) -> bool:
        if tag_filters:
            ev_tags = [str(t).lower() for t in (ev.get("tags") or [])]
            if not any(t in ev_tags or t == str(ev.get("kind") or "").lower() for t in tag_filters):
                return False
        if not qtext:
            return True
        blob = " ".join(
            [
                str(ev.get("summary") or ""),
                str(ev.get("kind") or ""),
                " ".join(ev.get("tags") or []),
                json.dumps(ev.get("payload") or {}, ensure_ascii=False),
            ]
        ).lower()
        # all whitespace-separated tokens must appear (AND)
        tokens = [t for t in re.split(r"\s+", qtext) if t]
        return all(tok in blob for tok in tokens)

    matched = [ev for ev in items if _match(ev)]
    return matched[:limit]


def retrieve_events(user_id: str, *, query: str = "", tags: Optional[list[str]] = None,
                    limit: int = 6) -> list[dict[str, Any]]:
    """Keyword overlap, with newest first on ties. Filter before limiting.

    Chinese bigrams support partial matches without an external tokenizer.
    The candidate pool is bounded to 500 matching events for this user.
    """
    from sqlalchemy import or_
    from app.data_access import current_user
    current_user()

    uid = str(user_id or "").strip()
    words = re.findall(r"[a-z0-9_+#.-]+|[\u3400-\u9fff]+", query.lower())
    terms = set()
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word) and len(word) > 1:
            terms.update(word[i:i + 2] for i in range(len(word) - 1))
        elif len(word) > 1:
            terms.add(word)
    terms = sorted(terms)[:32]
    tag_terms = [str(t).lower() for t in (tags or []) if str(t).strip()][:16]
    if not uid or limit <= 0 or not (terms or tag_terms):
        return []
    _ensure_table()
    with memory_session() as session:
        q = select(EpisodicEvent).where(EpisodicEvent.user_id == uid)
        columns = (EpisodicEvent.summary, EpisodicEvent.tags_json, EpisodicEvent.kind)
        if terms:
            q = q.where(or_(*(c.icontains(t, autoescape=True) for t in terms for c in columns)))
        if tag_terms:
            q = q.where(or_(*(EpisodicEvent.tags_json.icontains(
                json.dumps(t, ensure_ascii=False), autoescape=True) for t in tag_terms)))
        rows = session.execute(q.order_by(EpisodicEvent.created_at.desc(), EpisodicEvent.id.desc())
                               .limit(500)).scalars().all()
        items = [_row_dict(row) for row in rows]
    def score(event):
        blob = " ".join([event["summary"], event["kind"], *event["tags"]]).lower()
        return sum(term in blob for term in terms)
    return sorted(items, key=score, reverse=True)[:min(limit, 100)]


def recent_events(user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    return search_events(user_id, limit=limit)


def list_events(user_id: str, *, kind: Optional[str] = None, since: Optional[datetime] = None,
                limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first events for one user, filtered by kind and a lower time bound.

    Read-only and scoped to the caller; used by domain summaries (e.g. the
    sport training log) that need a date range rather than keyword search.
    """
    _ensure_table()
    uid = str(user_id or "").strip()
    if not uid:
        return []
    limit = max(1, min(int(limit or 200), 1000))
    with memory_session() as session:
        query = select(EpisodicEvent).where(EpisodicEvent.user_id == uid)
        if kind:
            query = query.where(EpisodicEvent.kind == str(kind))
        if since is not None:
            query = query.where(EpisodicEvent.created_at >= since)
        query = query.order_by(EpisodicEvent.created_at.desc(), EpisodicEvent.id.desc()).limit(limit)
        return [_row_dict(row) for row in session.execute(query).scalars().all()]


@tool('memory_get', risk_level='read', description='get event')
def get_event(event_id: int) -> Optional[dict[str, Any]]:
    """Load one episodic row. Returns None when the id does not exist."""
    from app.data_access import current_user
    current_user()
    try:
        _ensure_table()
        with memory_session() as session:
            row = session.get(EpisodicEvent, int(event_id))
            return _row_dict(row) if row is not None else None
    except Exception:
        return None


@tool('memory_update', risk_level='self_write', description='update event summary')
def update_event_summary(event_id: int, user_id: str, summary: str) -> str:
    """Rewrite one event's summary.

    Returns ``ok``, ``missing``, or ``forbidden`` (row belongs to another user).
    """
    try:
        _ensure_table()
        uid = str(user_id or "").strip()
        if not uid:
            return "forbidden"
        with memory_session() as session:
            row = session.get(EpisodicEvent, int(event_id))
            if row is None:
                return "missing"
            if row.user_id != uid:
                return "forbidden"
            row.summary = (summary or "")[:2000]
            session.flush()
            return "ok"
    except Exception:
        return "missing"


@tool('memory_delete', risk_level='self_write', description='delete event')
def delete_event(event_id: int, user_id: str) -> str:
    """Delete one event. Returns ``ok``, ``missing``, or ``forbidden``."""
    try:
        _ensure_table()
        uid = str(user_id or "").strip()
        if not uid:
            return "forbidden"
        with memory_session() as session:
            row = session.get(EpisodicEvent, int(event_id))
            if row is None:
                return "missing"
            if row.user_id != uid:
                return "forbidden"
            session.delete(row)
            session.flush()
            return "ok"
    except Exception:
        return "missing"


@tool('memory_delete_all', risk_level='self_write', description='delete user events')
def delete_user_events(user_id: str) -> int:
    """Delete every episodic event for one user. Returns the number removed."""
    from sqlalchemy import delete as sql_delete

    try:
        _ensure_table()
        uid = str(user_id or "").strip()
        if not uid:
            return 0
        with memory_session() as session:
            result = session.execute(sql_delete(EpisodicEvent).where(EpisodicEvent.user_id == uid))
            session.flush()
            return int(result.rowcount or 0)
    except Exception:
        return 0


def format_context(
    user_id: str,
    *,
    query: str = "",
    tags: Optional[list[str]] = None,
    limit: int = 8,
) -> str:
    """Compact Chinese context block for LLM prompts / analyze."""
    events = search_events(user_id, query=query, tags=tags, limit=limit)
    if not events:
        return ""
    lines = ["【近期记忆】"]
    for ev in events:
        when = (ev.get("created_at") or "")[:19].replace("T", " ")
        kind = ev.get("kind") or ""
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        lines.append(f"- ({when}) [{kind}] {summary}")
    return "\n".join(lines)
