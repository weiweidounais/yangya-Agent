"""LangGraph Personal Agent flow with human-in-the-loop confirm + agent loop.

Nodes: chat, draft → await_confirm (interrupt), update_profile, wrap_up.
HTTP APIs stay on ``run_action`` unchanged.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent import cost_ledger, tool_registry
from app.agent.budget_tiers import classify_difficulty
from app.agent.harness.traces import record_step
from app.agent.loop import (
    continue_card,
    default_loop_fields,
    ensure_loop,
    extend_budget,
    loop_snapshot,
    near_limit,
    progress_report,
    resume_loop_fields,
)
from app.agent.node_wrap import is_stopped, wrap_node
from app.agent.router import route_from_start, route_intent
from app.agent.subgraphs import (
    node_await_confirm,
    node_chat,
    node_draft,
    node_update_profile,
)
from app.state import AgentState

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CKPT_PATH = DATA_DIR / "checkpoints.db"


def node_wrap_up(state: AgentState) -> dict[str, Any]:
    """Near the budget limit: hand over what is done instead of starting the next stage."""
    near = near_limit(state) or "max_steps"
    reason = f"wrap_up:{near}"
    report = progress_report(state, near, wrap_up=True)
    record_step(
        thread_id=str(state.get("thread_id") or ""),
        user_id=str(state.get("user_id") or ""),
        step=int(state.get("loop_step") or 0),
        intent=str(state.get("action") or ""),
        tool="loop_wrap_up",
        detail={"reason": reason, "tier": state.get("loop_tier")},
    )
    return {
        "error": "loop_budget:wrap_up",
        "loop_stop_reason": reason,
        "pending_continue": continue_card(state, reason),
        "messages": [{"role": "assistant", "content": report}],
    }


def _after_draft(state: AgentState) -> str:
    if state.get("error") or is_stopped(state):
        return "end"
    return "await_confirm"


def _after_confirm(state: AgentState) -> str:
    """Confirm → end; reject → auto redraft within budget, else end."""
    if state.get("confirmed") is False and state.get("pending_redraft"):
        return "draft"
    return "end"


def build_graph():
    tool_registry.ensure_defaults()
    g = StateGraph(AgentState)
    g.add_node("update_profile", wrap_node("update_profile", node_update_profile))
    g.add_node("draft", wrap_node("draft", node_draft))
    g.add_node("await_confirm", wrap_node("await_confirm", node_await_confirm))
    g.add_node("chat", wrap_node("chat", node_chat))
    g.add_node("wrap_up", node_wrap_up)

    g.add_conditional_edges(
        START,
        route_from_start,
        {
            "update_profile": "update_profile",
            "draft": "draft",
            "await_confirm": "await_confirm",
            "chat": "chat",
        },
    )
    g.add_edge("update_profile", END)
    g.add_edge("chat", END)
    g.add_edge("wrap_up", END)
    g.add_conditional_edges(
        "draft",
        _after_draft,
        {"await_confirm": "await_confirm", "end": END},
    )
    g.add_conditional_edges(
        "await_confirm",
        _after_confirm,
        {"draft": "draft", "end": END},
    )
    return g


_checkpointer: Optional[SqliteSaver] = None
_compiled = None


def get_checkpointer() -> SqliteSaver:
    global _checkpointer
    if _checkpointer is None:
        from app.data_access.checkpoints import open_saver
        _checkpointer = open_saver(CKPT_PATH)
    return _checkpointer


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph().compile(checkpointer=get_checkpointer())
    return _compiled


def new_thread_id() -> str:
    return uuid.uuid4().hex


def get_thread_snapshot(thread_id: str) -> dict[str, Any]:
    """Return whether a LangGraph checkpoint exists for thread_id (+ owner).

    Used by /api/confirm: nonexistent threads must not confirm anything.
    """
    from app.data_access import current_user
    current_user()
    tid = str(thread_id or "").strip()
    if not tid:
        return {"exists": False, "thread_id": tid, "user_id": None, "values": {}}
    graph = get_graph()
    config = {"configurable": {"thread_id": tid}}
    try:
        snap = graph.get_state(config)
    except Exception as exc:
        return {
            "exists": False,
            "thread_id": tid,
            "user_id": None,
            "values": {},
            "error": type(exc).__name__,
        }
    values = dict(snap.values or {}) if snap and snap.values else {}
    exists = bool(values)
    return {
        "exists": exists,
        "thread_id": tid,
        "user_id": (values.get("user_id") or None) if exists else None,
        "confirmed": values.get("confirmed") if exists else None,
        "has_draft": bool(values.get("draft")) if exists else False,
        "next": list(snap.next or ()) if snap else [],
        "values": values,
    }


def _failure_state(result: dict[str, Any]) -> dict[str, Any]:
    from app.agent.node_wrap import NODE_FAILED, GUARD_UNAVAILABLE, _fail_closed

    code = GUARD_UNAVAILABLE if str(result.get("error") or "").startswith(GUARD_UNAVAILABLE) else NODE_FAILED
    return {
        **result,
        **_fail_closed(code, result),
        "confirmed": False, "confirm_payload": {}, "draft": {},
        "pending_redraft": False, "pending_continue": None,
        "ask": None,
    }


def failure_exit(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    """Retire dependent cards while preserving results of completed steps."""
    state = _failure_state(result)
    tid = str(thread_id)
    try:
        graph = get_graph()
        config = {"configurable": {"thread_id": tid}}
        if graph.get_state(config).values:
            # This terminal node has only an END edge, cancelling pending tasks.
            graph.update_state(config, state, as_node="wrap_up")
    except Exception:
        # Storage outages are reported safely; restart recovery is out of scope.
        pass
    return {"thread_id": thread_id, "state": state, "status": "error", "awaiting_confirm": False,
            "interrupt": None, "next": []}


def staged_graph_call(tid: str, operation) -> dict[str, Any]:
    """Nodes commit independently; a later failure preserves completed steps."""
    from app.agent.node_wrap import NodeWriteFailure, NODE_FAILED, GUARD_UNAVAILABLE

    try:
        out = operation()
        if str(out["state"].get("error") or "").startswith((NODE_FAILED, GUARD_UNAVAILABLE)):
            return failure_exit(tid, out["state"])
        return out
    except ContinueError:
        raise
    except Exception as exc:
        result = exc.result if isinstance(exc, NodeWriteFailure) else {"error": NODE_FAILED}
        return failure_exit(tid, result)


def update_thread_state(thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
    """Checkpoint-only edits use the same failure boundary as graph execution."""
    def update():
        graph = get_graph()
        config = {"configurable": {"thread_id": thread_id}}
        graph.update_state(config, values)
        return _pack(thread_id, values, graph, config)

    return staged_graph_call(thread_id, update)


def run_action(payload: dict[str, Any], *, thread_id: Optional[str] = None) -> dict[str, Any]:
    from app.data_access import current_user
    uid = current_user()
    if uid is not None:
        payload = {**payload, "user_id": uid}
    tid = thread_id or payload.get("thread_id") or new_thread_id()
    return staged_graph_call(tid, lambda: _run_action(payload, thread_id=tid))


def _run_action(payload: dict[str, Any], *, thread_id: Optional[str] = None) -> dict[str, Any]:
    """Invoke or resume the graph. Returns a normalized result dict."""
    graph = get_graph()
    tid = thread_id or payload.get("thread_id") or new_thread_id()
    config = {"configurable": {"thread_id": tid}}

    if "thread_id" not in payload:
        payload = {**payload, "thread_id": tid}

    if payload.get("_resume") is None:
        payload.setdefault("intent_result", None)
        payload.setdefault("intent_message", "")
        if os.getenv("INTENT_SCORING_MODE", "rules") == "hybrid":
            # A fresh turn must overwrite the previous checkpoint's crisis flag.
            # Explicit current-turn detections remain authoritative; resumes keep it.
            payload.setdefault("crisis_detected", False)
            payload["intent_input_fields"] = [field for field in ("message",) if field in payload]
            payload.setdefault("message", "")
        route_intent(payload)

    # Seed loop fields on first invoke (resume keeps checkpoint values)
    if payload.get("_resume") is None:
        tier = payload.get("loop_tier") or task_tier(payload)
        defaults = default_loop_fields(tier)
        seeded = ensure_loop({**defaults, **payload})
        for k, v in defaults.items():
            if k not in payload or payload.get(k) is None:
                payload[k] = seeded.get(k, v)
        payload.setdefault("error", None)
        payload.setdefault("pending_continue", None)
        payload.setdefault("loop_continued", False)
        payload.setdefault("loop_task_payload", _task_payload(payload))

    if payload.get("_resume") is not None:
        # Human wait time is not agent work: restart the wall clock on resume.
        result = graph.invoke(
            Command(resume=payload["_resume"], update=resume_loop_fields()),
            config=config,
        )
        return _pack(tid, result, graph, config)

    result = graph.invoke(payload, config=config)
    return _pack(tid, result, graph, config)


def task_tier(payload: dict[str, Any]) -> str:
    """Budget tier for a new task — same difficulty classifier as model routing."""
    try:
        intent = str(route_intent(payload) or payload.get("action") or "chat")
    except Exception:
        intent = str(payload.get("action") or "chat")
    return classify_difficulty(intent)


def _task_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The original request, kept on the checkpoint so "continue" can re-run it."""
    return {
        k: v
        for k, v in payload.items()
        if not k.startswith(("loop_", "_"))
        and k not in ("thread_id", "error", "pending_continue", "messages")
    }


_CARRY_KEYS = (
    "loop_step",
    "loop_tokens",
    "loop_cost_units",
    "loop_tier",
    "loop_escalated",
    "loop_escalation",
    "loop_redraft_count",
    "loop_retries",
    "loop_feedback",
    "profile",
    "draft_request",
)


class ContinueError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code, self.message, self.http_status = code, message, http_status


_continue_locks: dict[str, threading.Lock] = {}
_continue_locks_guard = threading.Lock()


def continue_task(thread_id: str, user_id: str) -> dict[str, Any]:
    """User clicked "continue" on a budget card: +50% of the tier, then re-run.

    Allowed any number of times; each click is still bounded by the user's
    daily / monthly cost cap. A cost-cap stop resumes without extra budget once
    the cap has reset.
    """
    tid = str(thread_id or "").strip()
    with _continue_locks_guard:
        lock = _continue_locks.setdefault(tid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ContinueError("continue_in_progress", "这个任务正在继续中，请稍等", 409)
    try:
        return staged_graph_call(tid, lambda: _continue_task(tid, user_id))
    finally:
        lock.release()


def _continue_task(tid: str, user_id: str) -> dict[str, Any]:
    snap = get_thread_snapshot(tid)
    if not snap.get("exists"):
        raise ContinueError("thread_not_found", "找不到这个任务", 404)
    if str(snap.get("user_id") or "") != user_id:
        raise ContinueError("thread_forbidden", "thread_id 不属于当前用户", 403)
    values = dict(snap.get("values") or {})
    if snap.get("next"):
        raise ContinueError("awaiting_confirm", "这个任务在等你确认卡，不需要继续")
    card = values.get("pending_continue") or {}
    if not card or not str(values.get("error") or "").startswith("loop_budget"):
        raise ContinueError("nothing_to_continue", "这个任务没有停在预算上限，不需要继续")

    graph = get_graph()
    config = {"configurable": {"thread_id": tid}}
    cap = cost_ledger.check(user_id)
    if not cap.ok:
        msg = cost_ledger.cap_message(cap)
        new_card = continue_card(values, f"cost_cap:{cap.scope}", kind="cost_cap", cap=cap.as_dict())
        if "seconds" in card.get("spent", {}):
            new_card["spent"]["seconds"] = card["spent"]["seconds"]
        graph.update_state(config, {"pending_continue": new_card})
        state = {**values, "pending_continue": new_card, "messages": [{"role": "assistant", "content": msg}]}
        out = _pack(tid, state, graph, config)
        out["status"] = "cost_cap"
        return out

    budget = values.get("loop_budget") or {}
    now = time.time()
    elapsed = max(0.0, float(card.get("spent", {}).get(
        "seconds", now - float(values.get("loop_started_at") or now)
    )))
    # Overruns consume the old allowance, never the new extension.
    old_limit = float(budget.get("max_seconds") or 0.0)
    consumed = min(elapsed, old_limit) if old_limit > 0 else 0.0
    extended = card.get("kind") != "cost_cap"
    if extended:
        budget = extend_budget(values)
    task = dict(values.get("loop_task_payload") or {})
    n = int(values.get("loop_continues") or 0) + 1
    payload: dict[str, Any] = {
        **task,
        **{k: values[k] for k in _CARRY_KEYS if values.get(k) is not None},
        "user_id": user_id,
        "thread_id": tid,
        "loop_budget": budget,
        "loop_started_at": now - consumed,
        "loop_stop_reason": None,
        "loop_last_error": None,
        "loop_history": [],
        "loop_wrap_up": False,
        "loop_continues": n,
        "loop_continued": True,
        "loop_task_payload": task,
        "pending_continue": None,
        "error": None,
    }
    record_step(
        thread_id=tid,
        user_id=user_id,
        step=int(values.get("loop_step") or 0),
        intent=str(task.get("action") or ""),
        tool="loop_continue",
        detail={"continues": n, "extended": extended, "budget": budget, "tier": values.get("loop_tier")},
    )
    result = graph.invoke(payload, config=config)
    out = _pack(tid, result, graph, config)
    out["status"] = "continued"
    return out


def _pack(tid: str, result: dict[str, Any], graph, config) -> dict[str, Any]:
    snap = graph.get_state(config)
    awaiting = bool(snap.next) and "await_confirm" in (snap.next or ())
    interrupt_payload = None
    if snap.tasks:
        for task in snap.tasks:
            ints = getattr(task, "interrupts", None) or ()
            for it in ints:
                interrupt_payload = getattr(it, "value", it)
                awaiting = True
                break
    state = dict(result) if result else {}
    return {
        "thread_id": tid,
        "state": state,
        "awaiting_confirm": awaiting,
        "interrupt": interrupt_payload,
        "next": list(snap.next or ()),
        "intent": route_intent(state),
        "loop": loop_snapshot(state),
    }
