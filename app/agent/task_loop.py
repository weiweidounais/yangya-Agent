"""observe → plan → tool → validate loop for a single agent task.

Each iteration:
  observe  — gather facts for this attempt (state + feedback so far)
  plan     — decide which tool to call and with what arguments
  tool     — execute the planned call
  validate — check the output; a failure becomes a retry reason

Failed validations feed their reason into the next attempt (max N retries).
The task budget (steps / estimated tokens / cost / wall clock, tiered by task
difficulty) and the user's cost cap are checked before every iteration. Near
the limit the task escalates once, then wraps up with the partial output; at
the limit it stops gracefully with a progress report and a "continue" card.
Runaway patterns (repeated calls / errors, no progress, captcha or risk-control
signals from external tools) stop the loop regardless of budget.

External side-effect tools never run without an explicit user confirmation
already present on state — the loop cannot grant itself permission.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from app.agent import cost_ledger, token_meter
from app.agent.loop import (
    budget_from_state,
    check_budget_or_escalate,
    continue_card,
    ensure_loop,
    near_limit,
    progress_report,
)
from app.agent.runaway import (
    EXTERNAL_TOOLS,  # act outside the user's workspace; only after a confirm card
    args_signature,
    detect,
    has_risk_signal,
    is_external_task,
    make_entry,
    runaway_text,
)


class ConfirmRequired(PermissionError):
    """Raised when a plan asks for an external tool without user confirmation."""


@dataclass
class Plan:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    finish: bool = False  # plan-level early exit (e.g. need to ask the user)


@dataclass
class Validation:
    ok: bool
    reason: str = ""

    @classmethod
    def passed(cls) -> "Validation":
        return cls(True, "")

    @classmethod
    def failed(cls, reason: str) -> "Validation":
        return cls(False, reason or "校验未通过")


@dataclass
class LoopContext:
    state: dict[str, Any]
    attempt: int
    feedback: list[dict[str, str]]
    trace: list[dict[str, Any]]

    def reasons(self) -> list[str]:
        return [str(f.get("reason") or "") for f in self.feedback if f.get("reason")]


@dataclass
class TaskResult:
    # done / exhausted (retries used up) / stopped (budget or cost cap) /
    # wrapped_up (near budget limit, partial output) / runaway (loop detector) /
    # blocked (needs confirmation) / finished_early (plan asked the user)
    status: Literal["done", "exhausted", "stopped", "wrapped_up", "runaway", "blocked", "finished_early"]
    output: Any = None
    attempts: int = 0
    steps: int = 0
    tokens: int = 0
    feedback: list[dict[str, str]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: Optional[str] = None
    message: str = ""
    tokens_after: Optional[int] = None  # task total incl. tokens spent earlier in the node
    history: Optional[list[dict[str, Any]]] = None
    escalation: dict[str, Any] = field(default_factory=dict)
    card: Optional[dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status == "done"

    @property
    def stop_error(self) -> str:
        """``error`` value for a stop status ('' when the loop did not stop)."""
        if self.status == "stopped":
            return f"loop_budget:{'cost_cap' if str(self.stop_reason).startswith('cost_cap') else self.stop_reason}"
        if self.status == "runaway":
            return f"loop_runaway:{str(self.stop_reason or '').removeprefix('runaway:')}"
        return ""

    def loop_patch(self, state: dict[str, Any]) -> dict[str, Any]:
        """Loop counters to merge back onto AgentState."""
        st = {**ensure_loop(state), **self.escalation}
        budget = budget_from_state(st)
        tokens = (
            self.tokens_after
            if self.tokens_after is not None
            else int(st.get("loop_tokens") or 0) + self.tokens
        )
        out: dict[str, Any] = {
            "loop_step": int(st.get("loop_step") or 0) + self.steps,
            "loop_tokens": tokens,
            "loop_cost_units": budget.cost_of(tokens),
            "loop_feedback": list(self.feedback)[-10:],
        }
        out.update(self.escalation)
        if self.history is not None:
            out["loop_history"] = list(self.history)
        if self.stop_reason:
            out["loop_stop_reason"] = self.stop_reason
        if self.status == "wrapped_up":
            out["loop_wrap_up"] = True
        if self.card is not None:
            out["pending_continue"] = self.card
        return out


ObserveFn = Callable[[LoopContext], dict[str, Any]]
PlanFn = Callable[[LoopContext, dict[str, Any]], Plan]
ActFn = Callable[[LoopContext, Plan], Any]
ValidateFn = Callable[[LoopContext, Plan, Any], Validation]


def guard_external(tool: str, confirmed: bool) -> None:
    if tool in EXTERNAL_TOOLS and confirmed is not True:
        raise ConfirmRequired(f"{tool} 需要用户先在确认卡上确认")


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    try:
        return copy.deepcopy(state)
    except Exception:  # noqa: BLE001 — non-copyable values: shallow copy still isolates keys
        return dict(state)


def run_task_loop(
    *,
    name: str,
    state: dict[str, Any],
    observe: ObserveFn,
    plan: PlanFn,
    act: ActFn,
    validate: ValidateFn,
    max_attempts: Optional[int] = None,
    feedback: Optional[list[dict[str, str]]] = None,
) -> TaskResult:
    """Run the cycle until validation passes, retries run out, or budget stops it.

    ``max_attempts`` defaults to ``1 + AGENT_MAX_RETRIES``.

    Token budget includes usage already metered by the enclosing node (e.g. a
    crisis classifier call before the loop), not just the checkpoint value.
    Callbacks receive a copy of state; confirmation is fixed at loop start.
    """
    st = {**dict(state or {}), **ensure_loop(state)}
    confirmed_at_start = st.get("confirmed") is True
    parent = token_meter.current()
    budget = budget_from_state(st)
    attempts_cap = max_attempts if max_attempts is not None else 1 + max(0, budget.max_retries)
    attempts_cap = max(1, int(attempts_cap))
    fb: list[dict[str, str]] = list(feedback if feedback is not None else st.get("loop_feedback") or [])
    trace: list[dict[str, Any]] = []
    base_step = int(st.get("loop_step") or 0)
    parent_tokens = parent.tokens if parent else 0
    base_tokens = int(st.get("loop_tokens") or 0) + parent_tokens
    uid = str(st.get("user_id") or "")
    history: list[dict[str, Any]] = list(st.get("loop_history") or [])
    seen_outputs: set[str] = set()
    escalation: dict[str, Any] = {}
    last_output: Any = None
    steps = 0

    with token_meter.metered() as meter:

        def _live() -> dict[str, Any]:
            live = dict(st)
            live["loop_step"] = base_step + steps
            live["loop_tokens"] = base_tokens + meter.tokens
            live["loop_cost_units"] = budget.cost_of(live["loop_tokens"])
            return live

        def _result(status, **kw) -> TaskResult:
            return TaskResult(
                status=status,
                output=kw.pop("output", last_output),
                attempts=len(trace),
                steps=steps,
                tokens=meter.tokens,
                feedback=fb,
                trace=trace,
                tokens_after=base_tokens + meter.tokens,
                history=history,
                escalation=escalation,
                **kw,
            )

        def _done_items() -> list[str]:
            return [f"{name} 已尝试 {len(trace)} 次"] if trace else []

        def _runaway(kind: str) -> TaskResult:
            return _result(
                "runaway",
                stop_reason=f"runaway:{kind}",
                message=progress_report(_live(), None, done=_done_items(), headline=runaway_text(kind)),
            )

        for attempt in range(1, attempts_cap + 1):
            if uid:
                cap = cost_ledger.check(uid, extra=cost_ledger.cny_for_tokens(parent_tokens + meter.tokens))
                if not cap.ok:
                    why = f"cost_cap:{cap.scope}"
                    return _result(
                        "stopped",
                        stop_reason=why,
                        message=progress_report(
                            _live(), None, done=_done_items(), headline=cost_ledger.cap_message(cap)
                        ),
                        card=continue_card(_live(), why, kind="cost_cap", cap=cap.as_dict()),
                    )
            ok, reason, esc = check_budget_or_escalate(_live(), where=name)
            if esc:
                escalation.update(esc)
                st.update(esc)
                budget = budget_from_state(st)
            if not ok:
                return _result(
                    "stopped",
                    stop_reason=reason,
                    message=progress_report(_live(), reason, done=_done_items()),
                    card=continue_card(_live(), reason),
                )
            if attempt > 1:
                hit = detect(history)
                if hit:
                    return _runaway(hit[0])
                near = near_limit(_live())
                if near and last_output is not None:
                    return _result(
                        "wrapped_up",
                        stop_reason=f"wrap_up:{near}",
                        message=progress_report(_live(), near, done=_done_items(), wrap_up=True),
                    )

            ctx = LoopContext(state=_snapshot(st), attempt=attempt, feedback=fb, trace=trace)
            row: dict[str, Any] = {"attempt": attempt, "task": name}
            obs = observe(ctx) or {}
            row["observed"] = sorted(obs.keys())
            p = plan(ctx, obs)
            row["plan"] = {"tool": p.tool, "note": p.note}
            if p.finish:
                steps += 1
                trace.append(row)
                return _result("finished_early", output=p.args, message=p.note)

            try:
                guard_external(p.tool, confirmed_at_start)
            except ConfirmRequired as exc:
                row["blocked"] = str(exc)
                trace.append(row)
                return _result("blocked", output=None, message=str(exc))

            try:
                out = act(ctx, p)
                v = validate(ctx, p, out)
            except ConfirmRequired:
                raise
            except Exception as exc:  # noqa: BLE001 — tool failure is a retry reason
                out = None
                v = Validation.failed(f"{p.tool} 执行出错：{type(exc).__name__}: {exc}"[:300])
            steps += 1
            last_output = out if out is not None else last_output
            row["valid"] = v.ok
            if not v.ok:
                row["reason"] = v.reason
                fb.append({"source": "validate", "reason": v.reason})
            trace.append(row)
            fp = args_signature("out", out)
            new_data = out is not None and fp not in seen_outputs
            seen_outputs.add(fp)
            risky = is_external_task(p.tool) and has_risk_signal(out, "" if v.ok else v.reason)
            history.append(
                make_entry(
                    p.tool,
                    p.args,
                    error="" if v.ok else v.reason,
                    progress=v.ok or new_data,
                    risk=risky,
                    source="task",
                )
            )
            if risky:
                return _runaway("risk_signal")
            if v.ok:
                return _result("done", output=out)

        return _result("exhausted", message=f"{name} 重试 {attempts_cap} 次后仍未通过校验")
