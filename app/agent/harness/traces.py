"""Harness trajectory log: one structured, redacted row per agent step (SQLite).

Columns: input summary, tool, params, result summary, latency, model tier/model,
token estimates, LLM call breakdown, guardrail hits, and the OTel trace / span id
of the step (links a row to its Langfuse trace). Queryable by thread / user
through the admin API only (see app.agent.harness.admin).

``user_id`` / ``thread_id`` are stored as keyed HMAC pseudonyms
(``identity.user_pseudonym`` / ``thread_pseudonym``). Queries accept either the raw
id or the pseudonym; rows written before pseudonymisation still match their raw id.

Writes are off the request path: ``record_step`` enqueues, a daemon thread
redacts and inserts in batches. Queue overflow and write failures are counted
(``writer_stats``) and logged instead of being silently dropped. Readers call
``flush`` first so queries see every step recorded before them.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, Float, Integer, String, Text, func, or_, select, text, true
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, init_db
from app.data_access.session import SessionLocal
from app.audit_db import AUDIT_ENGINE, bind_audit_table, ensure_audit_table
from app.agent.harness.identity import thread_pseudonym, user_pseudonym
from app.agent.harness.redact import redact_obj, redact_text

log = logging.getLogger("harness.traces")


class TraceStep(Base):
    __tablename__ = "agent_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    user_id: Mapped[str] = mapped_column(String(128), index=True, default="")
    step: Mapped[int] = mapped_column(Integer, default=0)
    intent: Mapped[str] = mapped_column(String(64), default="")
    tool: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[int] = mapped_column(Integer, default=1)  # 1=success, 0=failure, -1=outcome unknown
    error: Mapped[str] = mapped_column(Text, default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    input_summary: Mapped[str] = mapped_column(Text, default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    result_summary: Mapped[str] = mapped_column(Text, default="")
    tier: Mapped[str] = mapped_column(String(16), default="")
    tokens_in_est: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out_est: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls_json: Mapped[str] = mapped_column(Text, default="[]")
    guardrails_json: Mapped[str] = mapped_column(Text, default="[]")
    trace_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    span_id: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )


bind_audit_table(TraceStep.__table__)

# Columns added after the first release; older SQLite files get ALTER TABLE.
_ADDED_COLUMNS: dict[str, str] = {
    "input_summary": "TEXT DEFAULT ''",
    "params_json": "TEXT DEFAULT '{}'",
    "result_summary": "TEXT DEFAULT ''",
    "tier": "VARCHAR(16) DEFAULT ''",
    "tokens_in_est": "INTEGER DEFAULT 0",
    "tokens_out_est": "INTEGER DEFAULT 0",
    "llm_calls_json": "TEXT DEFAULT '[]'",
    "guardrails_json": "TEXT DEFAULT '[]'",
    "trace_id": "VARCHAR(32) DEFAULT ''",
    "span_id": "VARCHAR(16) DEFAULT ''",
}

_MAX_LLM_CALLS = 200
_ensured_engine: Any = None
_ensure_lock = threading.Lock()


def _ensure() -> None:
    global _ensured_engine
    with SessionLocal() as session:
        ENGINE = session.get_bind(mapper=TraceStep)

    if _ensured_engine is ENGINE:
        return
    with _ensure_lock:
        if _ensured_engine is ENGINE:
            return
        if ENGINE is AUDIT_ENGINE:
            ensure_audit_table(TraceStep.__table__)
        else:
            init_db()
            Base.metadata.create_all(ENGINE)
        with ENGINE.begin() as conn:
            have = {r[1] for r in conn.execute(text("PRAGMA table_info(agent_traces)"))}
            for col, ddl in _ADDED_COLUMNS.items():
                if col in have:
                    continue
                try:
                    conn.execute(text(f"ALTER TABLE agent_traces ADD COLUMN {col} {ddl}"))
                except OperationalError as exc:
                    # Another worker process migrated first.
                    if "duplicate column" not in str(exc).lower():
                        raise
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_traces_trace_id ON agent_traces (trace_id)"))
        _ensured_engine = ENGINE


def _loads(raw: Optional[str], default: Any) -> Any:
    try:
        val = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default
    return val if isinstance(val, type(default)) else default


def _dumps(obj: dict[str, Any], limit: int) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= limit:
        return s
    return json.dumps({"_truncated": True, "preview": s[: limit - 80]}, ensure_ascii=False)


def _dumps_list(items: list[Any], limit: int) -> tuple[str, int]:
    """JSON list that fits ``limit``; drops whole items from the end. Returns (json, dropped)."""
    kept = list(items)
    s = json.dumps(kept, ensure_ascii=False, default=str)
    while len(s) > limit and kept:
        over = len(s) - limit
        avg = max(1, len(s) // max(1, len(kept)))
        cut = max(1, min(len(kept), -(-over // avg)))
        kept = kept[: len(kept) - cut]
        s = json.dumps(kept, ensure_ascii=False, default=str)
    return s, len(items) - len(kept)


def _row_dict(row: TraceStep) -> dict[str, Any]:
    return {
        "id": row.id,
        "thread_id": row.thread_id or "",
        "user_id": row.user_id or "",
        "step": int(row.step or 0),
        "intent": row.intent or "",
        "tool": row.tool or "",
        "model": row.model or "",
        "tier": row.tier or "",
        "latency_ms": float(row.latency_ms or 0.0),
        "ok": None if row.ok == -1 else bool(row.ok),
        "error": row.error or "",
        "input_summary": row.input_summary or "",
        "params": _loads(row.params_json, {}),
        "result_summary": row.result_summary or "",
        "tokens_in_est": int(row.tokens_in_est or 0),
        "tokens_out_est": int(row.tokens_out_est or 0),
        "llm_calls": _loads(row.llm_calls_json, []),
        "guardrails": _loads(row.guardrails_json, []),
        "detail": _loads(row.detail_json, {}),
        "trace_id": row.trace_id or "",
        "span_id": row.span_id or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _sandbox_values_for(user_id: str) -> list[str]:
    if not user_id:
        return []
    try:
        from app.agent.harness.leak import sandbox_values
        from app.db import read_profile_data
        from app.data_access import user_scope

        # The writer thread has no request context. This uid belongs to the
        # server-recorded trace; scope only this record's redaction lookup.
        with user_scope(user_id):
            return sandbox_values(user_id, profile=read_profile_data(user_id))
    except Exception:
        return []


def _sensitive_guard(text: str, kinds: list[str]) -> str:
    """Once sensitive content is detected, never keep raw text: only kinds + a masked summary."""
    if not text:
        return ""
    try:
        from app.privacy import redact_sensitive

        _masked, hits = redact_sensitive(text, include_contact=False)
    except Exception:
        hits = []
    found = sorted(set(list(kinds) + list(hits)))
    if not found:
        return text
    return f"[redacted: {','.join(found)}] ({len(text)}字)"


def _build_row(rec: dict[str, Any]) -> Optional[TraceStep]:
    tid = str(rec.get("thread_id") or "").strip()
    if not tid:
        return None
    uid = str(rec.get("user_id") or "")
    hidden = list(rec.get("hidden") or []) + _sandbox_values_for(uid)
    calls = list(rec.get("llm_calls") or [])[:_MAX_LLM_CALLS]
    calls_red = redact_obj(calls, extra_values=hidden, max_items=_MAX_LLM_CALLS)
    calls_json, dropped = _dumps_list(calls_red, 12000)
    dropped += max(0, len(rec.get("llm_calls") or []) - _MAX_LLM_CALLS)
    guards_json, g_dropped = _dumps_list(
        redact_obj(list(rec.get("guardrails") or []), extra_values=hidden, max_items=100), 4000
    )
    detail = dict(rec.get("detail") or {})
    gate_kinds = [str(k) for k in (detail.get("kinds") or [])] if rec.get("intent") == "privacy_gate" or detail.get("kinds") else []
    rec = dict(rec)
    for _f in ("input_summary", "result_summary", "error"):
        rec[_f] = _sensitive_guard(str(rec.get(_f) or ""), gate_kinds if _f == "input_summary" else [])
    if dropped:
        detail["llm_calls_dropped"] = dropped
        detail["llm_calls_total"] = len(rec.get("llm_calls") or [])
    if g_dropped:
        detail["guardrails_dropped"] = g_dropped
    return TraceStep(
        thread_id=thread_pseudonym(tid),
        user_id=user_pseudonym(uid),
        step=int(rec.get("step") or 0),
        intent=str(rec.get("intent") or "")[:64],
        tool=str(rec.get("tool") or "")[:128],
        model=str(rec.get("model") or "")[:128],
        tier=str(rec.get("tier") or "")[:16],
        latency_ms=float(rec.get("latency_ms") or 0.0),
        ok=-1 if rec.get("ok") is None else (1 if rec.get("ok", True) else 0),
        error=redact_text(rec.get("error") or "", max_len=1000, extra_values=hidden),
        detail_json=_dumps(redact_obj(detail, extra_values=hidden), 4000),
        input_summary=redact_text(rec.get("input_summary") or "", max_len=600, extra_values=hidden),
        params_json=_dumps(redact_obj(rec.get("params") or {}, extra_values=hidden), 4000),
        result_summary=redact_text(rec.get("result_summary") or "", max_len=600, extra_values=hidden),
        tokens_in_est=int(rec.get("tokens_in_est") or 0),
        tokens_out_est=int(rec.get("tokens_out_est") or 0),
        llm_calls_json=calls_json,
        guardrails_json=guards_json,
        trace_id=str(rec.get("trace_id") or "")[:32],
        span_id=str(rec.get("span_id") or "")[:16],
        created_at=rec.get("created_at") or datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------- writer

_stats_lock = threading.Lock()
_stats: dict[str, Any] = {
    "enqueued": 0,
    "written": 0,
    "dropped_queue_full": 0,
    "write_errors": 0,
    "rows_lost": 0,
    "last_error": "",
    "last_error_at": None,
}


def _bump(**kw: int) -> None:
    with _stats_lock:
        for k, v in kw.items():
            _stats[k] = int(_stats.get(k) or 0) + v


def _note_error(exc: BaseException, lost: int) -> None:
    with _stats_lock:
        _stats["write_errors"] = int(_stats["write_errors"]) + 1
        _stats["rows_lost"] = int(_stats["rows_lost"]) + lost
        _stats["last_error"] = type(exc).__name__
        _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
    log.warning("trace write failed (%s); rows_lost=%d", type(exc).__name__, lost)


def _insert(recs: list[dict[str, Any]]) -> list[TraceStep]:
    """Insert a batch; on failure retry row-by-row so one bad row loses only itself."""
    _ensure()
    rows: list[TraceStep] = []
    for rec in recs:
        try:
            row = _build_row(rec)
        except Exception as exc:
            _note_error(exc, 1)
            continue
        if row is not None:
            rows.append(row)
    if not rows:
        return []
    try:
        with SessionLocal() as session:
            session.add_all(rows)
            session.commit()
            for r in rows:
                session.refresh(r)
            session.expunge_all()
        _bump(written=len(rows))
        return rows
    except Exception:
        pass
    done: list[TraceStep] = []
    for rec in recs:
        try:
            row = _build_row(rec)
            if row is None:
                continue
            with SessionLocal() as session:
                session.add(row)
                session.commit()
                session.refresh(row)
                session.expunge(row)
            done.append(row)
            _bump(written=1)
        except Exception as exc:
            _note_error(exc, 1)
    return done


def _queue_max() -> int:
    try:
        return max(100, int(os.getenv("TRACE_QUEUE_MAX") or 10000))
    except ValueError:
        return 10000


class _Writer:
    BATCH = 100
    INTERVAL_S = 0.2

    def __init__(self) -> None:
        self.q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=_queue_max())
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._pending = 0
        self._cv = threading.Condition()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="trace-writer", daemon=True)
            self._thread.start()

    def submit(self, rec: dict[str, Any]) -> bool:
        pending = False
        try:
            self._ensure_thread()
            with self._cv:
                self._pending += 1
                pending = True
            self.q.put_nowait(rec)
            pending = False
            _bump(enqueued=1)
            return True
        except Exception as exc:
            if pending:
                with self._cv:
                    self._pending -= 1
                    self._cv.notify_all()
            if isinstance(exc, queue.Full):
                _bump(dropped_queue_full=1)
                log.warning("trace queue full; dropping step tool=%s", rec.get("tool"))
            else:
                _note_error(exc, 1)
            return False

    def _run(self) -> None:
        while True:
            try:
                first = self.q.get()
            except Exception:
                continue
            batch = [first]
            deadline = time.monotonic() + self.INTERVAL_S
            while len(batch) < self.BATCH:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self.q.get(timeout=remaining))
                except queue.Empty:
                    break
            try:
                _insert(batch)
            except Exception as exc:
                _note_error(exc, len(batch))
            finally:
                with self._cv:
                    self._pending -= len(batch)
                    self._cv.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        end = time.monotonic() + timeout
        with self._cv:
            while self._pending > 0:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def pending(self) -> int:
        with self._cv:
            return self._pending


_writer = _Writer()
atexit.register(lambda: _writer.flush(timeout=3.0))


def _async_enabled() -> bool:
    return (os.getenv("TRACE_WRITE_MODE") or "async").strip().lower() != "sync"


def flush(timeout: float = 5.0) -> bool:
    """Block until queued steps are written (or timeout). True when drained."""
    return _writer.flush(timeout=timeout)


def writer_stats() -> dict[str, Any]:
    with _stats_lock:
        out = dict(_stats)
    out["pending"] = _writer.pending()
    out["mode"] = "async" if _async_enabled() else "sync"
    return out


def record_step(
    *,
    thread_id: str,
    user_id: str = "",
    step: int = 0,
    intent: str = "",
    tool: str = "",
    model: str = "",
    latency_ms: float = 0.0,
    ok: Optional[bool] = True,
    error: str = "",
    detail: Optional[dict[str, Any]] = None,
    input_summary: str = "",
    params: Optional[dict[str, Any]] = None,
    result_summary: str = "",
    tier: str = "",
    tokens_in_est: int = 0,
    tokens_out_est: int = 0,
    llm_calls: Optional[list[dict[str, Any]]] = None,
    guardrails: Optional[list[dict[str, Any]]] = None,
    hidden_values: Optional[list[str]] = None,
    trace_id: str = "",
    span_id: str = "",
    sync: Optional[bool] = None,
) -> dict[str, Any]:
    """Queue one trace step (redacted + written off the request path). Never raises.

    Returns ``{"queued": True}`` in async mode, the stored row in sync mode, ``{}``
    when the step is invalid or was dropped. ``trace_id`` / ``span_id`` default to
    the active OTel span (when tracing is on).
    """
    if not trace_id:
        trace_id, cur_span = _current_trace_ids()
        span_id = span_id or cur_span
    rec = {
        "thread_id": thread_id,
        "user_id": user_id,
        "step": step,
        "intent": intent,
        "tool": tool,
        "model": model,
        "latency_ms": latency_ms,
        "ok": ok,
        "error": error,
        "detail": dict(detail or {}),
        "input_summary": input_summary,
        "params": dict(params or {}),
        "result_summary": result_summary,
        "tier": tier,
        "tokens_in_est": tokens_in_est,
        "tokens_out_est": tokens_out_est,
        "llm_calls": list(llm_calls or []),
        "guardrails": list(guardrails or []),
        "hidden": list(hidden_values or []),
        "trace_id": trace_id,
        "span_id": span_id,
        "created_at": datetime.now(timezone.utc),
    }
    if not str(thread_id or "").strip():
        return {}
    use_sync = (not _async_enabled()) if sync is None else bool(sync)
    if not use_sync:
        return {"queued": True} if _writer.submit(rec) else {}
    try:
        rows = _insert([rec])
        return _row_dict(rows[0]) if rows else {}
    except Exception as exc:
        _note_error(exc, 1)
        return {}


def _current_trace_ids() -> tuple[str, str]:
    try:
        from app.observability.tracing import current_ids

        return current_ids()
    except Exception:
        return "", ""


def _id_cond(col: Any, raw: str, hashed: str) -> Any:
    raw = str(raw or "").strip()
    return or_(col == hashed, col == raw) if raw != hashed else col == hashed


def _conditions(
    *,
    thread_id: str = "",
    user_id: str = "",
    tool: str = "",
    ok: Optional[bool] = None,
    since: Optional[datetime] = None,
    trace_id: str = "",
) -> list[Any]:
    conds: list[Any] = []
    if thread_id:
        conds.append(_id_cond(TraceStep.thread_id, thread_id, thread_pseudonym(thread_id)))
    if user_id:
        conds.append(_id_cond(TraceStep.user_id, user_id, user_pseudonym(user_id)))
    if tool:
        conds.append(TraceStep.tool == str(tool).strip())
    if ok is not None:
        conds.append(TraceStep.ok == (1 if ok else 0))
    if since is not None:
        conds.append(TraceStep.created_at >= since)
    if trace_id:
        conds.append(TraceStep.trace_id == str(trace_id).strip())
    return conds


def summarize(conds: list[Any]) -> dict[str, Any]:
    """Aggregates over *every* row matching ``conds`` (not just one page)."""
    with SessionLocal() as session:
        agg = session.execute(
            select(
                func.count(TraceStep.id),
                func.coalesce(func.sum(TraceStep.latency_ms), 0.0),
                func.coalesce(func.sum(TraceStep.tokens_in_est), 0),
                func.coalesce(func.sum(TraceStep.tokens_out_est), 0),
                func.coalesce(func.sum(1 - TraceStep.ok), 0),
                func.coalesce(func.max(TraceStep.latency_ms), 0.0),
            ).where(*conds)
        ).one()
        valid_calls = func.json_valid(TraceStep.llm_calls_json) == 1
        valid_guards = func.json_valid(TraceStep.guardrails_json) == 1
        n_calls = session.execute(
            select(func.coalesce(func.sum(func.json_array_length(TraceStep.llm_calls_json)), 0)).where(
                *conds, valid_calls
            )
        ).scalar()
        n_guards = session.execute(
            select(func.coalesce(func.sum(func.json_array_length(TraceStep.guardrails_json)), 0)).where(
                *conds, valid_guards
            )
        ).scalar()
        je = func.json_each(TraceStep.llm_calls_json).table_valued("value").alias("call")
        n_failed = session.execute(
            select(func.count())
            .select_from(TraceStep)
            .join(je, true())
            .where(*conds, valid_calls, func.json_extract(je.c.value, "$.ok") == 0)
        ).scalar()
    steps = int(agg[0] or 0)
    return {
        "scope": "all",
        "steps": steps,
        "latency_ms": round(float(agg[1] or 0.0), 1),
        "avg_latency_ms": round(float(agg[1] or 0.0) / steps, 1) if steps else 0.0,
        "max_latency_ms": round(float(agg[5] or 0.0), 1),
        "tokens_in_est": int(agg[2] or 0),
        "tokens_out_est": int(agg[3] or 0),
        "errors": int(agg[4] or 0),
        "llm_calls": int(n_calls or 0),
        "llm_failed": int(n_failed or 0),
        "guardrail_hits": int(n_guards or 0),
    }


def page_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Same fields as ``summarize`` but only for the returned page."""
    steps = len(rows)
    lat = sum(r["latency_ms"] for r in rows)
    return {
        "scope": "page",
        "steps": steps,
        "latency_ms": round(lat, 1),
        "avg_latency_ms": round(lat / steps, 1) if steps else 0.0,
        "max_latency_ms": round(max((r["latency_ms"] for r in rows), default=0.0), 1),
        "tokens_in_est": sum(r["tokens_in_est"] for r in rows),
        "tokens_out_est": sum(r["tokens_out_est"] for r in rows),
        "errors": sum(1 for r in rows if r["ok"] is False),
        "unknown": sum(1 for r in rows if r["ok"] is None),
        "llm_calls": sum(len(r["llm_calls"]) for r in rows),
        "llm_failed": sum(1 for r in rows for c in r["llm_calls"] if c.get("ok") is False),
        "guardrail_hits": sum(len(r["guardrails"]) for r in rows),
    }


def query_traces(
    *,
    thread_id: str = "",
    user_id: str = "",
    tool: str = "",
    ok: Optional[bool] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
    order: str = "asc",
    trace_id: str = "",
    with_summary: bool = False,
) -> dict[str, Any]:
    """Filtered trace listing; returns {"traces": [...], "total": n[, "summary": {...}]}."""
    flush(timeout=2.0)
    _ensure()
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    conds = _conditions(thread_id=thread_id, user_id=user_id, tool=tool, ok=ok, since=since, trace_id=trace_id)
    with SessionLocal() as session:
        total = session.execute(select(func.count(TraceStep.id)).where(*conds)).scalar() or 0
        order_by = TraceStep.id.desc() if order == "desc" else TraceStep.id.asc()
        q = select(TraceStep).where(*conds).order_by(order_by).offset(offset).limit(limit)
        rows = list(session.execute(q).scalars().all())
    out: dict[str, Any] = {"traces": [_row_dict(r) for r in rows], "total": int(total)}
    if with_summary:
        out["summary"] = summarize(conds)
    return out


def list_traces(
    thread_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    tid = str(thread_id or "").strip()
    if not tid:
        return []
    return query_traces(thread_id=tid, limit=limit)["traces"]


def list_user_traces(user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    uid = str(user_id or "").strip()
    if not uid:
        return []
    return query_traces(user_id=uid, limit=limit, order="desc")["traces"]


def timed_call(fn, *args, **kwargs) -> tuple[Any, float, Optional[str]]:
    """Run fn; return (result|None, latency_ms, error_str|None)."""
    t0 = time.perf_counter()
    try:
        result = fn(*args, **kwargs)
        return result, (time.perf_counter() - t0) * 1000.0, None
    except Exception as exc:
        return None, (time.perf_counter() - t0) * 1000.0, f"{type(exc).__name__}: {exc}"[:500]
