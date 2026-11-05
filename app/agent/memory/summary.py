"""Dialogue summary → profile memory + episodic ``summary`` event.

The LLM call does not run on the request path. ``maybe_summarize_turn``
enqueues one job per thread and returns immediately. A newer turn for the
same thread replaces a job that has not started, and collapses into a
single follow-up when one is already running. With vector memory enabled,
every finished turn is retained for extraction instead of coalescing turns,
and each user's turns stay in submission order across retries.

Failures are logged at ERROR and retried. They are never logged as success.
Summary text stored on the profile is capped at 2000 characters.
"""

from __future__ import annotations

from app.agent.tool_registry import tool

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from app.prompts import load_prompt
from app.agent.harness.leak import register_system_prompt
from app.agent.memory import episodic, profile as profile_mem
from app.feature_flags import memory_vector_enabled

SUMMARY_SYSTEM = load_prompt("memory.summary", "v2")
register_system_prompt("memory.summary", SUMMARY_SYSTEM)

logger = logging.getLogger("app.agent.memory.summary")

SUMMARY_CAP = 2000
SUMMARY_EVALUATIVE_TERMS = ("诚信底线", "底线已确认", "明确拒绝数据造假", "拒绝造假", "道德评价")
_DEFAULT_RETRY_DELAY = 1.5
_DEFAULT_MAX_ATTEMPTS = 3


class SummaryFailed(Exception):
    """LLM failure. Logged as an error and retried; never treated as ok."""


@dataclass
class SummaryJob:
    user_id: str
    kind: str
    user_text: str
    assistant_text: str
    thread_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    attempt: int = 1
    not_before: float = 0.0
    memory_rev: int = 0
    turn_sequence: int = 0
    otel_ctx: Any = None  # request trace context, so background LLM spans stay in the same trace

    def key(self) -> str:
        tid = (self.thread_id or "").strip()
        if tid:
            return f"user:{self.user_id}:thread:{tid}"
        return f"user:{self.user_id}"


Generator = Callable[[SummaryJob], str]


class SummaryScheduler:
    """Single background worker. At most one in-flight summary per thread."""

    def __init__(
        self,
        *,
        retry_delay: float = _DEFAULT_RETRY_DELAY,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.retry_delay = float(retry_delay)
        self.max_attempts = max(1, int(max_attempts))
        self._cv = threading.Condition()
        self._pending: dict[str, SummaryJob] = {}
        self._inflight: set[str] = set()
        self._turn_sequence = 0
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._generator: Optional[Generator] = None

    def set_generator(self, fn: Optional[Generator]) -> None:
        self._generator = fn

    def submit(self, job: SummaryJob) -> dict[str, Any]:
        from app.data_access import current_user, NotFound
        uid = current_user()
        if uid is not None and uid != job.user_id:
            raise NotFound()
        job.memory_rev = int((profile_mem.get(job.user_id) or {}).get("memory_rev", 0))
        try:
            from opentelemetry import context as otel_context

            job.otel_ctx = otel_context.get_current()
        except Exception:
            job.otel_ctx = None
        from copy import deepcopy
        from app.data_access.namespaces import user_key
        key = user_key("summary", job.thread_id or "") if uid is not None else job.key()
        vector_enabled = memory_vector_enabled()
        if vector_enabled:
            # Extraction needs every finished turn, including intermediate turns
            # that the legacy latest-summary queue would otherwise coalesce.
            from uuid import uuid4
            key = f"{key}:turn:{uuid4().hex}"
        job = replace(job, extra=deepcopy(job.extra))
        with self._cv:
            if vector_enabled:
                self._turn_sequence += 1
                job.turn_sequence = self._turn_sequence
            self._ensure_worker()
            deduped = key in self._pending or key in self._inflight
            # Newest turn wins. A running job keeps the latest text as its only follow-up.
            job.attempt = 1
            job.not_before = 0.0
            self._pending[key] = job
            self._cv.notify()
        return {"ok": True, "scheduled": True, "deduped": deduped, "summary": ""}

    def _ensure_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="memory-summary", daemon=True)
        self._thread.start()

    def _ready_key(self) -> Optional[str]:
        now = time.monotonic()
        blocked_users = set()
        # Retries are reinserted into _pending; their original sequence keeps
        # later turns (even on another thread) behind the user's oldest turn.
        for key, job in sorted(self._pending.items(), key=lambda item: item[1].turn_sequence):
            if job.turn_sequence:
                if job.user_id in blocked_users:
                    continue
                blocked_users.add(job.user_id)
            if key in self._inflight:
                continue
            if job.not_before <= now:
                return key
        return None

    def _loop(self) -> None:
        while True:
            with self._cv:
                key = None if self._stop else self._ready_key()
                if key is None:
                    if self._stop and not self._inflight:
                        return
                    self._cv.wait(timeout=0.05)
                    continue
                job = self._pending.pop(key)
                self._inflight.add(key)
            try:
                result = self._execute(job)
            except Exception as exc:
                logger.error(
                    "summary failed user=%s thread=%s kind=%s attempt=%s err=%s",
                    job.user_id,
                    job.thread_id,
                    job.kind,
                    job.attempt,
                    type(exc).__name__,
                )
                result = {"ok": False, "reason": type(exc).__name__}
            if not result.get("ok"):
                self._schedule_retry(job, key, str(result.get("reason") or "failed"))
            with self._cv:
                self._inflight.discard(key)
                self._cv.notify_all()

    def _schedule_retry(self, job: SummaryJob, key: str, reason: str) -> None:
        with self._cv:
            if self._stop:
                return
            if key in self._pending:
                # A newer turn arrived while this one was failing. Keep the newer turn.
                return
            if job.attempt >= self.max_attempts:
                logger.error(
                    "summary gave up user=%s thread=%s kind=%s attempts=%s reason=%s",
                    job.user_id,
                    job.thread_id,
                    job.kind,
                    job.attempt,
                    reason,
                )
                return
            self._pending[key] = SummaryJob(
                user_id=job.user_id,
                kind=job.kind,
                user_text=job.user_text,
                assistant_text=job.assistant_text,
                thread_id=job.thread_id,
                extra=dict(job.extra or {}),
                memory_rev=job.memory_rev,
                turn_sequence=job.turn_sequence,
                otel_ctx=job.otel_ctx,
                attempt=job.attempt + 1,
                not_before=time.monotonic() + self.retry_delay,
            )
            self._cv.notify()

    def _execute(self, job: SummaryJob) -> dict[str, Any]:
        token = None
        try:
            from opentelemetry import context as otel_context

            if job.otel_ctx is not None:
                token = otel_context.attach(job.otel_ctx)
        except Exception:
            token = None
        try:
            from app.data_access import user_scope
            with user_scope(job.user_id):
                return self._execute_inner(job)
        finally:
            if token is not None:
                try:
                    otel_context.detach(token)
                except Exception:
                    pass

    def _execute_inner(self, job: SummaryJob) -> dict[str, Any]:
        try:
            decisions = []
            if memory_vector_enabled():
                from app.agent.memory import write_path
                job = write_path.redacted_turn(job)
                decisions = write_path.prepare_turn(job)
            produced = self._produce(job)
        except Exception as exc:
            logger.error(
                "summary failed user=%s thread=%s kind=%s attempt=%s err=%s",
                job.user_id,
                job.thread_id,
                job.kind,
                job.attempt,
                type(exc).__name__,
            )
            return {"ok": False, "reason": type(exc).__name__}

        from app.agent.memory.redact import redact_text

        from app.agent.harness.leak import memory_safe

        text = memory_safe(redact_text(str(produced or "").strip()), job.user_id)[:SUMMARY_CAP]
        if not text:
            text = redact_text(
                _fallback_summary(
                    kind=job.kind,
                    user_text=job.user_text,
                    assistant_text=job.assistant_text,
                )
            )[:SUMMARY_CAP]
        text = _filter_summary(text)
        if not text:
            text = f"[{job.kind}] （本轮摘要已省略敏感内容或评价）"
        text = memory_safe(text, job.user_id)[:SUMMARY_CAP]
        try:
            from app import db

            with db.memory_transaction():
                revision = int((profile_mem.get(job.user_id) or {}).get("memory_rev", 0))
                if revision != job.memory_rev:
                    logger.debug("stale summary dropped")
                    return {"ok": True, "dropped": True}
                merged = profile_mem.merge(
                    job.user_id,
                    {
                        "dialogue_summary": text,
                        "last_summary_kind": job.kind,
                    },
                )
                ev = episodic.write_event(
                    job.user_id,
                    "summary",
                    text,
                    tags=["summary", job.kind, "memory"],
                    payload={
                        "kind": job.kind,
                        "chars": len(text),
                        **(job.extra or {}),
                    },
                    thread_id=str(job.thread_id or ""),
                )
                if decisions:
                    write_path.apply_decisions(decisions, thread_id=job.thread_id,
                                               source_event_id=ev.get("id"))
        except Exception as exc:
            logger.error(
                "summary failed user=%s thread=%s kind=%s attempt=%s err=%s",
                job.user_id,
                job.thread_id,
                job.kind,
                job.attempt,
                type(exc).__name__,
            )
            return {"ok": False, "reason": "persist"}
        return {
            "ok": True,
            "summary": text,
            "event_id": ev.get("id"),
            "profile_keys": list(merged.keys()) if isinstance(merged, dict) else [],
        }

    def _produce(self, job: SummaryJob) -> str:
        if self._generator is not None:
            return self._generator(job)
        prior = _prior_summary(job.user_id)
        if memory_vector_enabled():
            from app.agent.memory.write_path import safe_text
            prior = safe_text(prior, job)
        return _generate_summary(
            kind=job.kind,
            user_text=job.user_text,
            assistant_text=job.assistant_text,
            prior=prior,
        )

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until queued work for this scheduler has finished. Tests and smoke only."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cv:
            while time.monotonic() < deadline:
                if not self._pending and not self._inflight:
                    return True
                remaining = deadline - time.monotonic()
                self._cv.wait(timeout=min(0.05, max(0.0, remaining)))
            return not self._pending and not self._inflight

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._pending.clear()
            self._cv.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=1.0)
        if thread is not None and thread.is_alive():
            # Keep ownership so reset cannot orphan a worker that still uses
            # the current database, generator, or test patches.
            raise TimeoutError("summary worker did not stop")
        self._thread = None


_scheduler = SummaryScheduler()


def get_scheduler() -> SummaryScheduler:
    return _scheduler


def reset_summary_scheduler(
    *,
    retry_delay: float = 0.0,
    max_attempts: int = 3,
) -> SummaryScheduler:
    """Replace the process scheduler. Used by tests."""
    global _scheduler
    _scheduler.shutdown()
    _scheduler = SummaryScheduler(retry_delay=retry_delay, max_attempts=max_attempts)
    return _scheduler


def flush_summaries(timeout: float = 5.0) -> bool:
    """Wait until the background summary queue is idle."""
    return _scheduler.flush(timeout)


@tool('summarize_turn', risk_level='self_write', description='Enqueue dialogue summary (background; does not block the turn)',
      tags=['memory', 'summary'], sensitive=False)
def maybe_summarize_turn(
    user_id: str,
    *,
    kind: str,
    user_text: str,
    assistant_text: str,
    thread_id: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Enqueue a turn summary and return without waiting for the LLM.

    ``ok`` here means the job was accepted, not that a summary was written.
    Generation failures are logged later at ERROR and retried.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return {"ok": False, "scheduled": False, "reason": "no_user"}
    ut = (user_text or "").strip()
    at = (assistant_text or "").strip()
    if not ut and not at:
        return {"ok": False, "scheduled": False, "reason": "empty"}
    job = SummaryJob(
        user_id=uid,
        kind=(kind or "chat").strip() or "chat",
        user_text=ut,
        assistant_text=at,
        thread_id=str(thread_id or ""),
        extra=dict(extra or {}),
    )
    from app.db import _memory_session, after_memory_commit

    if _memory_session.get() is not None:
        after_memory_commit(lambda: _scheduler.submit(job))
        return {"ok": True, "scheduled": True}
    return _scheduler.submit(job)


def _prior_summary(user_id: str) -> str:
    try:
        snap = profile_mem.get(user_id) or {}
        return str(snap.get("dialogue_summary") or "")[:1200]
    except Exception:
        return ""


def _generate_summary(
    *,
    kind: str,
    user_text: str,
    assistant_text: str,
    prior: str,
) -> str:
    from app.gateway.llm_gateway import chat_text, has_llm

    if not has_llm():
        return ""
    system = SUMMARY_SYSTEM
    user = (
        f"类型：{kind}\n"
        f"此前摘要：{prior or '（无）'}\n"
        f"用户：{user_text[:800]}\n"
        f"助手：{assistant_text[:800]}\n"
        "请给出更新后的简短记忆摘要。"
    )
    try:
        text = chat_text(
            system,
            user,
            task="summary",
            fallback="",
            timeout=20.0,
        )
    except Exception as exc:
        raise SummaryFailed(type(exc).__name__) from exc
    return (text or "").strip()[:SUMMARY_CAP]


def _filter_summary(text: str) -> str:
    """Drop evaluative sentences without discarding unrelated memory."""
    sentences = re.findall(r"[^。！？!?；;\n]+[。！？!?；;\n]*", text)
    kept = []
    dropped = 0
    for sentence in sentences:
        if any(term in sentence for term in SUMMARY_EVALUATIVE_TERMS):
            dropped += 1
        else:
            kept.append(sentence)
    if dropped:
        logger.info("summary filter dropped %d evaluative sentence(s)", dropped)
    return "".join(kept).strip()


def _fallback_summary(*, kind: str, user_text: str, assistant_text: str) -> str:
    u = (user_text or "").replace("\n", " ")[:120]
    a = (assistant_text or "").replace("\n", " ")[:120]
    return f"[{kind}] 用户：{u or '-'}；助手：{a or '-'}"
