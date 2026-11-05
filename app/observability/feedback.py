"""User thumbs up / down on assistant replies → local record + Langfuse score,
and admin one-click "add this trace to a dataset".

A feedback must reference a trace id (``X-Trace-Id`` of the chat response) whose
trace rows belong to the same user, so nobody can score another user's trace.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, init_db
from app.data_access.session import SessionLocal

log = logging.getLogger("observability.feedback")

SCORE_NAME = "user_feedback"


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    trace_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32), index=True)  # pseudonym
    thread_id: Mapped[str] = mapped_column(String(32), default="")  # pseudonym
    value: Mapped[int] = mapped_column(Integer)  # 1 up / -1 down
    comment: Mapped[str] = mapped_column(Text, default="")
    score_id: Mapped[str] = mapped_column(String(64), default="")
    langfuse: Mapped[str] = mapped_column(String(16), default="")  # queued | disabled
    dataset_item_id: Mapped[str] = mapped_column(String(64), default="")
    dataset: Mapped[str] = mapped_column(String(128), default="")


class FeedbackError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _value(raw: Any) -> int:
    if isinstance(raw, str):
        raw = raw.strip().lower()
        if raw in ("up", "+1", "1", "good", "like", "thumbs_up"):
            return 1
        if raw in ("down", "-1", "bad", "dislike", "thumbs_down"):
            return -1
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw in (1, -1):
        return int(raw)
    raise FeedbackError("bad_value", "value 只能是 up / down")


def _trace_rows(trace_id: str, user_pseudo: str) -> list[dict[str, Any]]:
    from app.agent.harness.traces import query_traces

    rows = query_traces(trace_id=trace_id, limit=50)["traces"]
    return [r for r in rows if r.get("user_id") == user_pseudo]


def record_feedback(*, user_id: str, trace_id: str, value: Any, comment: str = "", thread_id: str = "") -> dict[str, Any]:
    from app.agent.harness.identity import thread_pseudonym, user_pseudonym
    from app.agent.harness.redact import redact_text
    from app.observability import langfuse_client

    uid = str(user_id or "").strip()
    tid = str(trace_id or "").strip().lower()
    if not uid:
        raise FeedbackError("user_required", "需要 user_id")
    if len(tid) != 32 or any(c not in "0123456789abcdef" for c in tid):
        raise FeedbackError("bad_trace_id", "trace_id 格式不对")
    v = _value(value)
    upseudo = user_pseudonym(uid)
    if not _trace_rows(tid, upseudo):
        raise FeedbackError("trace_not_found", "找不到这条回复的轨迹", status=404)
    clean = redact_text(comment or "", max_len=500)
    score_id = str(uuid.uuid4())
    queued = langfuse_client.submit(
        lambda c, **kw: c.create_score(**kw),
        trace_id=tid,
        name=SCORE_NAME,
        value=float(v),
        data_type="NUMERIC",
        comment=clean,
        score_id=score_id,
        metadata={"source": "user", "thread": thread_pseudonym(thread_id) if thread_id else ""},
    )
    init_db()
    with SessionLocal() as s:
        row = Feedback(
            trace_id=tid,
            user_id=upseudo,
            thread_id=thread_pseudonym(thread_id) if thread_id else "",
            value=v,
            comment=clean,
            score_id=score_id,
            langfuse="queued" if queued else "disabled",
        )
        s.add(row)
        s.commit()
        return _row(row)


def _row(r: Feedback) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "trace_id": r.trace_id,
        "user_id": r.user_id,
        "thread_id": r.thread_id,
        "value": r.value,
        "comment": r.comment,
        "langfuse": r.langfuse,
        "score_id": r.score_id,
        "dataset": r.dataset,
        "dataset_item_id": r.dataset_item_id,
    }


def list_feedback(*, value: Optional[int] = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    init_db()
    conds = [Feedback.value == value] if value in (1, -1) else []
    with SessionLocal() as s:
        total = s.execute(select(func.count(Feedback.id)).where(*conds)).scalar() or 0
        ups = s.execute(select(func.count(Feedback.id)).where(Feedback.value == 1)).scalar() or 0
        downs = s.execute(select(func.count(Feedback.id)).where(Feedback.value == -1)).scalar() or 0
        rows = s.execute(
            select(Feedback).where(*conds).order_by(Feedback.id.desc()).offset(max(0, offset)).limit(max(1, min(limit, 500)))
        ).scalars().all()
        return {"total": int(total), "summary": {"scope": "all", "up": int(ups), "down": int(downs)}, "items": [_row(r) for r in rows]}


def _dataset_input(trace_id: str) -> dict[str, Any]:
    """Redacted trajectory of the trace (what was asked, which nodes ran, what came back)."""
    from app.agent.harness.traces import query_traces

    rows = query_traces(trace_id=trace_id, limit=50)["traces"]
    return {
        "steps": [
            {
                "tool": r["tool"],
                "intent": r["intent"],
                "input": r["input_summary"],
                "result": r["result_summary"],
                "guardrails": [g.get("type") or g.get("guardrail") for g in r["guardrails"]],
            }
            for r in rows
        ]
    }


def _candidates_dir() -> Path:
    from app.db import DATA_DIR

    from app.data_access.namespaces import user_directory
    d = user_directory(Path(DATA_DIR) / "golden_candidates")
    d.mkdir(parents=True, exist_ok=True)
    return d


def add_to_dataset(feedback_id: int, *, dataset: str = "", note: str = "", client: Any = None,
                   principal: Any = None) -> dict[str, Any]:
    """Langfuse dataset item from a feedback's trace; offline → golden-case candidate file."""
    from app.observability.config import langfuse_config
    from app.observability.langfuse_client import LangfuseClient

    init_db()
    with SessionLocal() as s:
        row = s.get(Feedback, int(feedback_id))
        if row is None:
            raise FeedbackError("feedback_not_found", "反馈不存在", status=404)
        fb = _row(row)
    cfg = langfuse_config()
    name = (dataset or cfg.feedback_dataset).strip()
    item_input = _dataset_input(fb["trace_id"])
    metadata = {
        "feedback_id": fb["id"],
        "feedback_value": fb["value"],
        "comment": fb["comment"],
        "note": note[:300],
        "source": "user_feedback",
    }
    item_id = f"fb-{fb['id']}-{fb['trace_id'][:12]}"
    lf = client or (LangfuseClient(cfg) if cfg.enabled else None)
    where = "langfuse" if lf is not None and lf.enabled else "local"
    target = {"dataset": name if where == "langfuse" else f"local:{name}", "dataset_item_id": item_id}
    if principal is not None:
        from app.admin.store import change, session

        # Commit the export intent before filesystem/network effects. An export
        # cannot be rolled back with SQL; failed attempts retain this journal row.
        with session(write=True) as s:
            change(s, principal, "feedback_dataset", fb["id"],
                   {key: fb[key] for key in target}, target)
    if lf is not None and lf.enabled:
        try:
            lf.create_dataset(name, description="Replies users rated (thumbs up / down)", metadata={"source": "feedback"})
        except Exception:
            pass  # usually "already exists"
        lf.create_dataset_item(
            dataset_name=name,
            input=item_input,
            metadata=metadata,
            item_id=item_id,
            source_trace_id=fb["trace_id"],
        )
        where = "langfuse"
    else:
        # Admin-authorized export: namespace by the stored record owner.
        from app.data_access import user_scope
        with user_scope(fb["user_id"]):
            path = _candidates_dir() / f"{item_id}.json"
        path.write_text(
            json.dumps(
                {
                    "id": item_id,
                    "category": "feedback_candidate",
                    "description": f"user feedback {fb['value']:+d}: {fb['comment']}",
                    "trace_id": fb["trace_id"],
                    "input": item_input,
                    "metadata": metadata,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        where = "local"
    with SessionLocal() as s:
        row = s.get(Feedback, int(feedback_id))
        row.dataset = name if where == "langfuse" else f"local:{name}"
        row.dataset_item_id = item_id
        s.commit()
    return {"dataset": name, "item_id": item_id, "stored": where, "trace_id": fb["trace_id"]}
