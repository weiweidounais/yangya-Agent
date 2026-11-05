"""Explicit agent loop: budgets, step tracking, redraft policy.

Loop state lives on AgentState / LangGraph checkpoint so it survives
HITL interrupts. Soft limits never raise — callers route to END / ask user.

A *task* is one inbound request plus its HITL resumes. Steps and tokens
accumulate across the task; the wall clock restarts on every resume so time
spent waiting for the human never counts against the agent.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Optional

from app.agent.budget_tiers import (
    MEDIUM,
    TIER_DEFAULTS,
    TIER_ZH,
    continue_extension,
    next_tier,
    normalize_tier,
    tier_limits,
)
from app.agent.cost_ledger import price_per_1k_tokens


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class LoopBudget:
    """Soft budgets for one task of the agent loop. 0 disables a dimension.

    Steps / seconds / tokens / cost come from the task's difficulty tier
    (see ``app.agent.budget_tiers``); retries and redrafts are tier-independent.
    """

    max_steps: int = TIER_DEFAULTS[MEDIUM][0]
    max_retries: int = 2
    max_redrafts: int = 1
    max_seconds: float = TIER_DEFAULTS[MEDIUM][1]
    max_tokens: int = TIER_DEFAULTS[MEDIUM][2]
    cost_per_1k_tokens: float = 0.0
    max_cost: float = 0.0
    tier: str = MEDIUM

    @classmethod
    def from_env(cls, tier: Optional[str] = None) -> "LoopBudget":
        t = normalize_tier(tier)
        limits = tier_limits(t)
        return cls(
            max_steps=int(limits["max_steps"]),
            max_retries=_int_env("AGENT_MAX_RETRIES", 2),
            max_redrafts=_int_env("AGENT_MAX_REDRAFTS", 1),
            max_seconds=float(limits["max_seconds"]),
            max_tokens=int(limits["max_tokens"]),
            cost_per_1k_tokens=price_per_1k_tokens(),
            max_cost=float(limits["max_cost"]),
            tier=t,
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def cost_of(self, tokens: int) -> float:
        return round(max(0, int(tokens)) / 1000.0 * max(0.0, self.cost_per_1k_tokens), 6)


def default_loop_fields(tier: Optional[str] = None) -> dict[str, Any]:
    budget = LoopBudget.from_env(tier)
    return {
        "loop_tier": budget.tier,
        "loop_escalated": False,
        "loop_escalation": None,
        "loop_step": 0,
        "loop_redraft_count": 0,
        "loop_retries": 0,
        "loop_started_at": time.time(),
        "loop_last_error": None,
        "loop_tokens": 0,
        "loop_cost_units": 0.0,
        "loop_budget": budget.as_dict(),
        "loop_stop_reason": None,
        "loop_feedback": [],
        "loop_history": [],
        "loop_wrap_up": False,
        "loop_continues": 0,
    }


def resume_loop_fields() -> dict[str, Any]:
    """Patch applied when a HITL interrupt resumes: new wall-clock segment."""
    return {"loop_started_at": time.time(), "loop_stop_reason": None}


def ensure_loop(state: dict[str, Any] | None) -> dict[str, Any]:
    """Return loop fields merged onto state defaults (does not mutate)."""
    state = dict(state or {})
    defaults = default_loop_fields(state.get("loop_tier"))
    out = {**defaults, **{k: state[k] for k in defaults if k in state and state[k] is not None}}
    if not out.get("loop_budget"):
        out["loop_budget"] = LoopBudget.from_env(out.get("loop_tier")).as_dict()
    if not out.get("loop_started_at"):
        out["loop_started_at"] = time.time()
    return out


def budget_from_state(state: dict[str, Any] | None) -> LoopBudget:
    st = state or {}
    raw = st.get("loop_budget") or {}
    tier = normalize_tier(st.get("loop_tier") or (raw.get("tier") if isinstance(raw, dict) else None))
    base = LoopBudget.from_env(tier)
    if not isinstance(raw, dict):
        return base
    return LoopBudget(
        tier=tier,
        max_steps=int(raw.get("max_steps", base.max_steps)),
        max_retries=int(raw.get("max_retries", base.max_retries)),
        max_redrafts=int(raw.get("max_redrafts", base.max_redrafts)),
        max_seconds=float(raw.get("max_seconds", base.max_seconds)),
        max_tokens=int(raw.get("max_tokens", base.max_tokens)),
        cost_per_1k_tokens=float(raw.get("cost_per_1k_tokens", base.cost_per_1k_tokens)),
        max_cost=float(raw.get("max_cost", raw.get("cost_soft_limit", base.max_cost))),
    )


def check_budget(state: dict[str, Any] | None) -> tuple[bool, Optional[str]]:
    """Return (ok, stop_reason). Soft limits — True means may continue."""
    st = ensure_loop(state)
    budget = budget_from_state(st)
    step = int(st.get("loop_step") or 0)
    if budget.max_steps > 0 and step >= budget.max_steps:
        return False, "max_steps"
    started = float(st.get("loop_started_at") or time.time())
    if budget.max_seconds > 0 and (time.time() - started) >= budget.max_seconds:
        return False, "max_seconds"
    tokens = int(st.get("loop_tokens") or 0)
    if budget.max_tokens > 0 and tokens >= budget.max_tokens:
        return False, "max_tokens"
    cost = float(st.get("loop_cost_units") or 0.0)
    if budget.max_cost > 0 and cost >= budget.max_cost:
        return False, "max_cost"
    return True, None


def bump_step(
    state: dict[str, Any] | None,
    *,
    error: Optional[str] = None,
    tokens_delta: int = 0,
    steps: int = 1,
) -> dict[str, Any]:
    """Increment step counter; optionally record last error / token usage."""
    st = ensure_loop(state)
    budget = budget_from_state(st)
    st["loop_step"] = int(st.get("loop_step") or 0) + max(0, int(steps))
    if error:
        st["loop_last_error"] = str(error)[:500]
        st["loop_retries"] = int(st.get("loop_retries") or 0) + 1
    if tokens_delta:
        st["loop_tokens"] = int(st.get("loop_tokens") or 0) + max(0, int(tokens_delta))
    st["loop_cost_units"] = budget.cost_of(int(st.get("loop_tokens") or 0))
    ok, reason = check_budget(st)
    if not ok:
        st["loop_stop_reason"] = reason
    return {
        "loop_step": st["loop_step"],
        "loop_redraft_count": int(st.get("loop_redraft_count") or 0),
        "loop_retries": int(st.get("loop_retries") or 0),
        "loop_started_at": st.get("loop_started_at"),
        "loop_last_error": st.get("loop_last_error"),
        "loop_tokens": int(st.get("loop_tokens") or 0),
        "loop_cost_units": st.get("loop_cost_units"),
        "loop_budget": st.get("loop_budget"),
        "loop_stop_reason": st.get("loop_stop_reason"),
        "loop_tier": st.get("loop_tier"),
        "loop_escalated": bool(st.get("loop_escalated")),
        "loop_escalation": st.get("loop_escalation"),
        "loop_continues": int(st.get("loop_continues") or 0),
        "loop_wrap_up": bool(st.get("loop_wrap_up")),
    }


ESCALATION_KEYS = ("loop_tier", "loop_budget", "loop_escalated", "loop_escalation", "loop_stop_reason")


def escalation_enabled() -> bool:
    return (os.getenv("AGENT_TIER_ESCALATION") or "1").strip().lower() not in ("0", "false", "no", "off")


def can_escalate(state: dict[str, Any] | None) -> bool:
    st = state or {}
    return (
        escalation_enabled()
        and not st.get("loop_escalated")
        and next_tier(budget_from_state(st).tier) is not None
    )


def escalate_tier(state: dict[str, Any] | None, *, reason: str) -> dict[str, Any]:
    """Move the task one tier up (at most once per task). Returns a state patch or {}.

    Counters (steps, tokens, wall clock) keep running; only the limits grow.
    The escalation is written to the step trace.
    """
    st = {**dict(state or {}), **ensure_loop(state)}
    if not can_escalate(st):
        return {}
    old = budget_from_state(st)
    target = next_tier(old.tier)
    assert target is not None
    new = LoopBudget.from_env(target)
    new.max_retries, new.max_redrafts = old.max_retries, old.max_redrafts
    event = {
        "from": old.tier,
        "to": target,
        "reason": str(reason or "")[:200],
        "step": int(st.get("loop_step") or 0),
        "tokens": int(st.get("loop_tokens") or 0),
        "at": time.time(),
    }
    _trace_escalation(state or {}, event)
    return {
        "loop_tier": target,
        "loop_budget": new.as_dict(),
        "loop_escalated": True,
        "loop_escalation": event,
        "loop_stop_reason": None,
    }


def escalate_if_harder(state: dict[str, Any] | None, detected: str, *, reason: str) -> dict[str, Any]:
    """Escalate when a node detects the task is harder than its current tier."""
    from app.agent.budget_tiers import tier_rank

    if tier_rank(detected) <= tier_rank(budget_from_state(state).tier):
        return {}
    return escalate_tier(state, reason=reason)


def check_budget_or_escalate(
    state: dict[str, Any] | None, *, where: str = ""
) -> tuple[bool, Optional[str], dict[str, Any]]:
    """Budget check that spends the task's one escalation before stopping.

    Nearing or hitting a tier's ceiling means the task is harder than it was
    classified, so the first time that happens the task moves up one tier.
    Returns (ok, stop_reason, escalation_patch).
    """
    ok, reason = check_budget(state)
    near = near_limit(state) if ok else None
    if ok and not near:
        return True, None, {}
    why = reason or f"near:{near}"
    patch = escalate_tier(state, reason=f"budget:{why}" + (f"@{where}" if where else ""))
    if not patch:
        return ok, reason, {}
    ok2, reason2 = check_budget({**dict(state or {}), **patch})
    return ok2, reason2, patch


NEAR_FRACTION = 0.8
NEAR_STEPS_LEFT = 2


def near_limit(state: dict[str, Any] | None) -> Optional[str]:
    """Dimension that is ≥80% used (or ≤2 steps left), else None."""
    st = ensure_loop(state)
    b = budget_from_state(st)
    step = int(st.get("loop_step") or 0)
    if b.max_steps > 0 and (b.max_steps - step <= NEAR_STEPS_LEFT or step >= NEAR_FRACTION * b.max_steps):
        return "max_steps"
    if b.max_seconds > 0:
        started = float(st.get("loop_started_at") or time.time())
        if time.time() - started >= NEAR_FRACTION * b.max_seconds:
            return "max_seconds"
    if b.max_tokens > 0 and int(st.get("loop_tokens") or 0) >= NEAR_FRACTION * b.max_tokens:
        return "max_tokens"
    if b.max_cost > 0 and float(st.get("loop_cost_units") or 0.0) >= NEAR_FRACTION * b.max_cost:
        return "max_cost"
    return None


def extend_budget(state: dict[str, Any] | None) -> dict[str, Any]:
    """Budget after one "continue" click: + 50% of the current tier's base."""
    st = ensure_loop(state)
    b = budget_from_state(st)
    ext = continue_extension(b.tier)
    new = LoopBudget(**{**b.as_dict()})
    if new.max_steps > 0:
        new.max_steps = int(new.max_steps + ext["max_steps"])
    if new.max_seconds > 0:
        new.max_seconds = float(new.max_seconds + ext["max_seconds"])
    if new.max_tokens > 0:
        new.max_tokens = int(new.max_tokens + ext["max_tokens"])
    if new.max_cost > 0:
        new.max_cost = float(new.max_cost + ext["max_cost"])
    return new.as_dict()


def continue_card(
    state: dict[str, Any] | None,
    reason: Optional[str],
    *,
    kind: str = "budget",
    cap: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Card shown at the limit: what is done, what was spent (¥), what continuing costs."""
    from app.agent.cost_ledger import check as cap_check
    from app.agent.cost_ledger import cny_for_tokens

    st = {**dict(state or {}), **ensure_loop(state)}
    b = budget_from_state(st)
    done, todo = _progress_items(st)
    ext = continue_extension(b.tier)
    tokens = int(st.get("loop_tokens") or 0)
    uid = str(st.get("user_id") or "")
    user_cost = cap if cap is not None else (cap_check(uid).as_dict() if uid else None)
    allowed = kind == "budget" and (user_cost is None or bool(user_cost.get("ok", True)))
    return {
        "type": "continue_budget",
        "kind": kind,  # budget | cost_cap
        "thread_id": st.get("thread_id"),
        "reason": reason,
        "tier": b.tier,
        "tier_zh": TIER_ZH.get(b.tier, b.tier),
        "escalated": bool(st.get("loop_escalated")),
        "continues": int(st.get("loop_continues") or 0),
        "done": done,
        "todo": todo,
        "spent": {
            "steps": int(st.get("loop_step") or 0),
            "seconds": max(0.0, time.time() - float(st.get("loop_started_at") or time.time())),
            "tokens": tokens,
            "cost_cny": cny_for_tokens(tokens),
        },
        "extension": {
            "steps": int(ext["max_steps"]),
            "seconds": float(ext["max_seconds"]),
            "tokens": int(ext["max_tokens"]),
        },
        "estimate_cny": cny_for_tokens(int(ext["max_tokens"])),
        "user_cost": user_cost,
        "can_continue": allowed,
        "resume_after": (user_cost or {}).get("reset_at") if kind == "cost_cap" else None,
    }


def _trace_escalation(state: dict[str, Any], event: dict[str, Any]) -> None:
    try:
        from app.agent.harness.traces import record_step

        record_step(
            thread_id=str(state.get("thread_id") or ""),
            user_id=str(state.get("user_id") or ""),
            step=int(event.get("step") or 0),
            intent=str(state.get("action") or state.get("intent") or ""),
            tool="loop_escalate",
            ok=True,
            detail={"kind": "tier_escalation", **{k: v for k, v in event.items() if k != "at"}},
        )
    except Exception:
        pass


def can_redraft(state: dict[str, Any] | None) -> bool:
    st = ensure_loop(state)
    budget = budget_from_state(st)
    ok, _ = check_budget(st)
    if not ok:
        return False
    return int(st.get("loop_redraft_count") or 0) < budget.max_redrafts


def bump_redraft(state: dict[str, Any] | None) -> dict[str, Any]:
    st = ensure_loop(state)
    fields = bump_step(st)
    fields["loop_redraft_count"] = int(st.get("loop_redraft_count") or 0) + 1
    return fields


def should_retry_tool(state: dict[str, Any] | None) -> bool:
    st = ensure_loop(state)
    budget = budget_from_state(st)
    return int(st.get("loop_retries") or 0) < budget.max_retries


def add_feedback(
    state: dict[str, Any] | None, *, source: str, reason: str
) -> list[dict[str, str]]:
    """Append a retry reason (user reject / validation) to the task feedback."""
    prev = list((state or {}).get("loop_feedback") or [])
    text = (reason or "").strip()[:500]
    if text:
        prev.append({"source": source, "reason": text})
    return prev[-10:]


_STOP_REASON_ZH = {
    "max_steps": "这次任务的步骤数到上限了（{max_steps} 步）",
    "max_seconds": "这次任务用时到上限了（约 {max_seconds:.0f} 秒）",
    "max_tokens": "这次任务的预计用量到上限了（约 {max_tokens} tokens）",
    "max_cost": "这次任务的预计费用到上限了（¥{max_cost}）",
}
_NEAR_REASON_ZH = {
    "max_steps": "这次任务的步骤数快到上限了",
    "max_seconds": "这次任务用时快到上限了",
    "max_tokens": "这次任务的预计用量快到上限了",
    "max_cost": "这次任务的预计费用快到上限了",
}


def stop_reason_text(reason: Optional[str], budget: LoopBudget) -> str:
    tpl = _STOP_REASON_ZH.get(str(reason or ""), "这次任务的预算用完了")
    try:
        return tpl.format(**budget.as_dict())
    except (KeyError, ValueError):
        return tpl


def _progress_items(state: dict[str, Any]) -> tuple[list[str], list[str]]:
    done: list[str] = []
    todo: list[str] = []
    profile = state.get("profile") or {}
    if any(profile.get(k) for k in ("name", "city", "occupation", "interests", "preferences", "facts")):
        done.append("读取了你的档案")
    draft = state.get("draft") or {}
    request = state.get("draft_request") or ""
    if draft.get("body"):
        done.append("写好了一版草稿（还没发出，也没替你用在任何地方）")
    elif request or state.get("action") == "draft":
        todo.append("写出草稿")
    if draft.get("body") and state.get("confirmed") is not True:
        todo.append("等你在确认卡上确认草稿")
    return done, todo


def progress_report(
    state: dict[str, Any] | None,
    reason: Optional[str],
    *,
    done: Optional[list[str]] = None,
    todo: Optional[list[str]] = None,
    wrap_up: bool = False,
    headline: Optional[str] = None,
) -> str:
    """User-facing graceful stop: why we paused, what is done, what is left."""
    st = dict(state or {})
    budget = budget_from_state(st)
    auto_done, auto_todo = _progress_items(st)
    done_items = list(done or []) + [d for d in auto_done if d not in (done or [])]
    todo_items = list(todo or []) + [t for t in auto_todo if t not in (todo or [])]
    if headline:
        lines = [headline]
    elif wrap_up:
        why = _NEAR_REASON_ZH.get(str(reason or ""), "这次任务的预算快用完了")
        lines = [f"{why}，我先把做好的部分交给你，不硬撑着出一个糊弄的结果。"]
    else:
        lines = [f"先停一下：{stop_reason_text(reason, budget)}，我不想硬撑着给你一个糊弄的结果。"]
    if done_items:
        lines.append("目前已经完成：" + "；".join(done_items) + "。")
    if todo_items:
        lines.append("还没做完：" + "；".join(todo_items) + "。")
    tail = "没有任何内容被自动发出。"
    if headline is None:
        from app.agent.cost_ledger import cny_for_tokens

        est = cny_for_tokens(int(continue_extension(budget.tier)["max_tokens"]))
        tail += f"想接着做就点「继续」，会再追加当前档位 50% 的预算（预计最多 ¥{est:.2f}）；或者把范围说小一点也行。"
    lines.append(tail)
    return "\n".join(lines)


def loop_snapshot(state: dict[str, Any] | None) -> dict[str, Any]:
    st = ensure_loop(state)
    ok, reason = check_budget(st)
    return {
        "step": int(st.get("loop_step") or 0),
        "redraft_count": int(st.get("loop_redraft_count") or 0),
        "retries": int(st.get("loop_retries") or 0),
        "last_error": st.get("loop_last_error"),
        "tokens": int(st.get("loop_tokens") or 0),
        "cost_units": float(st.get("loop_cost_units") or 0.0),
        "budget": st.get("loop_budget"),
        "within_budget": ok,
        "stop_reason": st.get("loop_stop_reason") or reason,
        "started_at": st.get("loop_started_at"),
        "feedback": list(st.get("loop_feedback") or []),
        "tier": budget_from_state(st).tier,
        "escalated": bool(st.get("loop_escalated")),
        "escalation": st.get("loop_escalation"),
        "continues": int(st.get("loop_continues") or 0),
        "wrap_up": bool(st.get("loop_wrap_up")),
        "near_limit": near_limit(st),
        "continue_card": (state or {}).get("pending_continue"),
    }
