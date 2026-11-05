"""Wrap LangGraph nodes with loop step, budgets, runaway detection + traces."""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from langgraph.errors import GraphInterrupt

from app.agent import cost_ledger, runaway, token_meter
from app.agent.harness import context as trace_ctx
from app.agent.harness.guardrails import guard_node_output
from app.agent.harness.redact import redact_text
from app.agent.harness.traces import record_step
from app.agent.loop import (
    bump_step,
    check_budget_or_escalate,
    continue_card,
    ensure_loop,
    near_limit,
    progress_report,
)
from app.agent.router import route_intent
from app.observability import tracing

# Nodes that only process a human decision; never budget-blocked, otherwise a
# late confirm/reject would be silently dropped.
HUMAN_DECISION_NODES = frozenset({"await_confirm"})

STOP_PREFIXES = ("loop_budget", "loop_runaway", "E_NODE_FAILED", "E_GUARD_UNAVAILABLE")

_LOOP_KEYS = (
    "loop_step",
    "loop_redraft_count",
    "loop_retries",
    "loop_started_at",
    "loop_last_error",
    "loop_tokens",
    "loop_cost_units",
    "loop_budget",
    "loop_stop_reason",
    "loop_tier",
    "loop_escalated",
    "loop_escalation",
    "loop_history",
    "loop_continues",
)


NodeFn = Callable[[dict[str, Any]], dict[str, Any]]


def _crisis_turn(state: dict[str, Any]) -> bool:
    """Crisis messages always reach the safety reply, whatever the budget or cap."""
    if os.getenv("INTENT_SCORING_MODE", "rules") == "hybrid":
        result = state.get("intent_result") or {}
        if result.get("rule_id") == "crisis" and result.get("safety_override"):
            return True
    msg = str(state.get("message") or "")
    if not msg:
        return False
    from app.agent.subgraphs.chat import has_crisis_signal

    return has_crisis_signal(msg)


def is_stopped(state: dict[str, Any]) -> bool:
    return str(state.get("error") or "").startswith(STOP_PREFIXES)


def _stop(st: dict[str, Any], *, error: str, reason: str, message: str, card=None) -> dict[str, Any]:
    out = {k: st[k] for k in _LOOP_KEYS if k in st}
    out.update(
        {
            "loop_stop_reason": reason,
            "error": error,
            "messages": [{"role": "assistant", "content": message}],
        }
    )
    if card is not None:
        out["pending_continue"] = card
    if error.startswith("loop_budget:"):
        record_step(
            thread_id=str(st.get("thread_id") or ""),
            user_id=str(st.get("user_id") or ""),
            step=int(st.get("loop_step") or 0),
            tool="loop_budget",
            ok=False,
            error=error,
            detail={"budget_stop": reason},
            guardrails=[{"guardrail": "loop_budget", "reason": reason}],
        )
    return out


def _trace(state: dict[str, Any], step: int, tool: str, detail: dict[str, Any], ok: bool = True) -> None:
    record_step(
        thread_id=str(state.get("thread_id") or ""),
        user_id=str(state.get("user_id") or ""),
        step=step,
        intent=str(state.get("action") or ""),
        tool=tool,
        ok=ok,
        detail=detail,
    )


def _safe(text: str, hidden: list[str], n: int) -> str:
    """Redact the full text first so truncation can never cut a secret in half."""
    return redact_text(text, extra_values=hidden)[:n]


def _chars(v: Any) -> int:
    return len(v) if isinstance(v, str) else 0


def _input_summary(state: dict[str, Any], hidden: list[str]) -> str:
    from app.agent.harness.traces import _sensitive_guard

    return _sensitive_guard(_input_summary_raw(state, hidden), [])


def _input_summary_raw(state: dict[str, Any], hidden: list[str]) -> str:
    parts = [f"action={state.get('action') or '-'}"]
    msg = state.get("message")
    if isinstance(msg, str) and msg.strip():
        parts.append(f"message[{len(msg)}字]={_safe(msg.strip(), hidden, 80)}")
    request = state.get("draft_request")
    if isinstance(request, str) and request.strip() and request != msg:
        parts.append(f"request[{len(request)}字]={_safe(request.strip(), hidden, 60)}")
    if state.get("draft"):
        parts.append("has_draft")
    return " | ".join(parts)


def _params(tool_name: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "action": state.get("action"),
        "message_chars": _chars(state.get("message")),
        "request_chars": _chars(state.get("draft_request")),
        "has_profile": bool(state.get("profile")),
        "confirmed": state.get("confirmed"),
        "redraft": state.get("loop_redraft_count"),
    }


def _result_summary(result: dict[str, Any], hidden: list[str]) -> str:
    parts = [f"keys={','.join(sorted(k for k in result if not k.startswith('loop_')))}"]
    msgs = result.get("messages") or []
    if isinstance(msgs, list) and msgs:
        last = msgs[-1] if isinstance(msgs[-1], dict) else {}
        content = str(last.get("content") or "")
        parts.append(f"reply[{len(content)}字]={_safe(content, hidden, 120)}")
    draft = result.get("draft")
    if isinstance(draft, dict) and draft:
        parts.append(f"draft {draft.get('kind') or 'other'}[{_chars(draft.get('body'))}字]")
    if result.get("error"):
        parts.append(f"error={result.get('error')}")
    return " | ".join(parts)


GUARD_UNAVAILABLE = "E_GUARD_UNAVAILABLE"
NODE_FAILED = "E_NODE_FAILED"
_SAFE_REPLY = {
    GUARD_UNAVAILABLE: "抱歉，这次回复没能完成安全检查，已拦截未发送。请稍后重试。（错误码：E_GUARD_UNAVAILABLE）",
    NODE_FAILED: "抱歉，这一步处理失败，请稍后重试。（错误码：E_NODE_FAILED）",
}


def _fail_closed(code: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fixed safe payload: the client only ever sees an error code, never node output
    or exception text (those go, redacted, to the trace)."""
    out: dict[str, Any] = {
        "confirm_payload": {},
        "confirmed": False,
        "error": code,
        "error_code": code,
        "messages": [{"role": "assistant", "content": _SAFE_REPLY[code]}],
    }
    failed_step = (result or {}).get("failed_step")
    if failed_step:
        out["failed_step"] = failed_step
        out["messages"][0]["content"] = f"步骤 {failed_step} 失败。" + _SAFE_REPLY[code]
    tid = (result or {}).get("thread_id")
    if isinstance(tid, str) and tid:
        out["thread_id"] = tid
    return out


class NodeWriteFailure(Exception):
    """Carry the safe response out of a discarded business workspace."""

    def __init__(self, result):
        self.result = result


def _sandbox_for(uid: str, state: dict[str, Any]) -> list[str]:
    """Stored sandbox values plus any being set in this very request. Raises on lookup failure."""
    from app.agent.harness.leak import sandbox_from_profile, sandbox_values

    incoming = state.get("profile") if isinstance(state.get("profile"), dict) else {}
    return sandbox_values(uid, strict=True) + sandbox_from_profile(incoming or {})


def _last_reply(result: dict[str, Any]) -> str:
    msgs = result.get("messages") or []
    if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
        return str(msgs[-1].get("content") or "")
    return ""


def _annotate_node_span(
    tool_name: str,
    state: dict[str, Any],
    *,
    intent: str,
    step: int,
    latency: float,
    ok: bool,
    error_code: str,
    agg: dict[str, Any],
    guardrails: list[dict[str, Any]],
    input_summary: str,
    result_summary: str,
    paused: bool,
    reply: str = "",
) -> None:
    obs = tracing.current()
    if not obs.recording:
        return
    try:
        tracing.set_identity(user_id=str(state.get("user_id") or ""), thread_id=str(state.get("thread_id") or ""))
        kinds = sorted({str(g.get("type") or g.get("guardrail") or "") for g in guardrails} - {""})
        obs.input(input_summary).output(result_summary).meta(
            tool=tool_name,
            intent=intent,
            step=step,
            latency_ms=round(latency, 1),
            ok=ok,
            error_code=error_code,
            tier=agg.get("tier") or "",
            model=agg.get("model") or "",
            llm_calls=agg.get("llm_calls") or 0,
            llm_failed=agg.get("llm_failed") or 0,
            tokens_in_est=agg.get("tokens_in_est") or 0,
            tokens_out_est=agg.get("tokens_out_est") or 0,
            guardrail_hits=len(guardrails),
            guardrail_types=",".join(kinds),
            paused=paused,
        )
        msg = state.get("message") or state.get("draft_request")
        tracing.set_trace_io(
            input=str(msg) if isinstance(msg, str) and msg.strip() else None,
            output=reply or None,
        )
        tracing.tag_trace([f"node:{tool_name}", *(["guardrail_hit", *kinds] if guardrails else [])])
        if not ok:
            obs.level("ERROR", error_code or "node failed")
        elif agg.get("llm_failed") or guardrails:
            obs.level("WARNING", "degraded LLM call" if agg.get("llm_failed") else "guardrail hit")
    except Exception:
        pass


def wrap_node(tool_name: str, fn: NodeFn) -> NodeFn:
    """Increment loop step, enforce budgets / cost caps / runaway, write traces.

    Must not swallow ``GraphInterrupt`` (HITL confirm).
    """
    human = tool_name in HUMAN_DECISION_NODES

    def _write_trace(
        state: dict[str, Any],
        ctx: dict[str, Any],
        *,
        intent: str,
        step: int,
        latency: float,
        ok: bool,
        error: str,
        result_summary: str,
        detail: dict[str, Any],
        fallback_model: str = "",
        reply: str = "",
    ) -> None:
        calls = list(ctx.get("llm_calls") or [])
        hidden = list(ctx["meta"].get("sandbox") or [])
        agg = trace_ctx.summarize_llm_calls(calls)
        detail = dict(detail)
        if agg["llm_failed"]:
            detail["llm_failed"] = agg["llm_failed"]
            detail["degraded"] = True
        if agg["usage_prompt_tokens"] or agg["usage_completion_tokens"]:
            detail["usage"] = {
                "prompt_tokens": agg["usage_prompt_tokens"],
                "completion_tokens": agg["usage_completion_tokens"],
            }
        record_step(
            thread_id=str(state.get("thread_id") or ""),
            user_id=str(state.get("user_id") or ""),
            step=step,
            intent=intent,
            tool=tool_name,
            model=agg["model"] or fallback_model,
            tier=agg["tier"],
            latency_ms=latency,
            ok=ok,
            error=error,
            detail=detail,
            input_summary=_input_summary(state, hidden),
            params=_params(tool_name, state),
            result_summary=result_summary,
            tokens_in_est=agg["tokens_in_est"],
            tokens_out_est=agg["tokens_out_est"],
            llm_calls=calls,
            guardrails=list(ctx.get("guardrails") or []),
            hidden_values=hidden,
        )
        _annotate_node_span(
            tool_name,
            state,
            intent=intent,
            step=step,
            latency=latency,
            ok=ok,
            error_code=str(detail.get("error_code") or ("loop_budget" if detail.get("budget_stop") else "")),
            agg=agg,
            guardrails=list(ctx.get("guardrails") or []),
            input_summary=_input_summary(state, hidden),
            result_summary=result_summary,
            paused=bool(detail.get("paused")),
            reply=reply,
        )

    def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        tracing.set_identity(user_id=str(state.get("user_id") or ""), thread_id=str(state.get("thread_id") or ""))
        with tracing.observe(f"node.{tool_name}", kind="agent", metadata={"tool": tool_name}):
            from app.business_stage import stage_business_writes, BusinessCommitFailure

            # Any wrapped node can write business data, including through tools.
            # Audit storage remains independent of this rollback boundary.
            try:
                with stage_business_writes():
                    result = _run(state)
                    if str(result.get("error") or "").startswith((NODE_FAILED, GUARD_UNAVAILABLE)):
                        raise NodeWriteFailure(result)
                    return result
            except NodeWriteFailure as exc:
                return exc.result
            except BusinessCommitFailure:
                _trace(state, int(state.get("loop_step") or 0), tool_name,
                       {"error_code": NODE_FAILED, "stage": "business_commit"}, ok=False)
                return _fail_closed(NODE_FAILED, {"failed_step": tool_name})

    def _run(state: dict[str, Any]) -> dict[str, Any]:
        st = {**dict(state), **ensure_loop(state)}
        uid = str(state.get("user_id") or "")

        if not human and not _crisis_turn(state):
            cap = cost_ledger.check(uid) if uid else None
            if cap is not None and not cap.ok:
                _trace(st, int(st.get("loop_step") or 0), "loop_cost_cap", cap.as_dict(), ok=False)
                return _stop(
                    st,
                    error="loop_budget:cost_cap",
                    reason=f"cost_cap:{cap.scope}",
                    message=progress_report(st, None, headline=cost_ledger.cap_message(cap)),
                    card=continue_card(st, f"cost_cap:{cap.scope}", kind="cost_cap", cap=cap.as_dict()),
                )
            ok, reason, esc = check_budget_or_escalate(st, where=tool_name)
            if esc:
                st.update(esc)
                state = {**dict(state), **esc}
            if not ok:
                return _stop(
                    st,
                    error=f"loop_budget:{reason}",
                    reason=str(reason),
                    message=progress_report(st, reason),
                    card=continue_card(st, reason),
                )

        intent = ""
        try:
            intent = str(route_intent(state) or "")
        except Exception:
            intent = str(state.get("action") or "")

        sandbox_error = ""
        try:
            sandbox = _sandbox_for(uid, state)
        except Exception as exc:
            sandbox, sandbox_error = [], type(exc).__name__
        with trace_ctx.step_context(
            tool=tool_name,
            thread_id=str(state.get("thread_id") or ""),
            user_id=uid,
            sandbox=sandbox,
        ) as ctx:
            t0 = time.perf_counter()
            err: str | None = None
            internal_err = ""
            result: dict[str, Any] = {}
            meter_tokens = 0
            if sandbox_error:
                # Cannot know which values must stay private: do not run the node
                # (its prompts could carry them to the LLM) and emit nothing.
                trace_ctx.record_guardrail(
                    {"guardrail": "no_leak", "type": "guard_error", "stage": "sandbox_lookup",
                     "error": sandbox_error, "action": "fail_closed"}
                )
                internal_err = f"sandbox_lookup_failed: {sandbox_error}"
                err = GUARD_UNAVAILABLE
                result = _fail_closed(GUARD_UNAVAILABLE, {"failed_step": tool_name})
            else:
                try:
                    with token_meter.metered() as meter:
                        try:
                            result = fn(state) or {}
                        finally:
                            meter_tokens = meter.tokens
                    if not isinstance(result, dict):
                        result = {}
                except GraphInterrupt:
                    # HITL / interrupt must propagate; still count a step + trace "paused"
                    latency = (time.perf_counter() - t0) * 1000.0
                    loop_fields = bump_step(st, tokens_delta=meter_tokens)
                    cost_ledger.record_spend(uid, cost_ledger.cny_for_tokens(meter_tokens))
                    _write_trace(
                        state,
                        ctx,
                        intent=intent,
                        step=int(loop_fields.get("loop_step") or 0),
                        latency=latency,
                        ok=True,
                        error="",
                        result_summary="paused: interrupt (awaiting human confirm)",
                        detail={"paused": "interrupt", "action": state.get("action")},
                    )
                    raise
                except Exception as exc:
                    internal_err = f"{type(exc).__name__}: {exc}"
                    err = NODE_FAILED
                    result = _fail_closed(NODE_FAILED, {"failed_step": tool_name})

            inputs = tuple(str(state.get(k) or "") for k in ("message", "draft_request"))
            if not sandbox_error:
                try:
                    result, _hits = guard_node_output(
                        result, user_id=uid, inputs=inputs, sandbox=sandbox
                    )
                except Exception as exc:
                    trace_ctx.record_guardrail(
                        {"guardrail": "no_leak", "type": "guard_error", "stage": "output_scan",
                         "error": type(exc).__name__, "action": "fail_closed"}
                    )
                    internal_err = internal_err or f"guard_failed: {type(exc).__name__}"
                    err = GUARD_UNAVAILABLE
                    result = _fail_closed(GUARD_UNAVAILABLE, {"failed_step": tool_name})

            latency = (time.perf_counter() - t0) * 1000.0
            if "loop_tier" in result and result.get("loop_escalated"):
                st.update({k: result[k] for k in ("loop_tier", "loop_budget", "loop_escalated", "loop_escalation")})
            loop_fields = bump_step(st, error=err, tokens_delta=meter_tokens)
            cost_ledger.record_spend(uid, cost_ledger.cny_for_tokens(meter_tokens))
            model = ""
            _write_trace(
                {**state, "thread_id": state.get("thread_id") or result.get("thread_id") or ""},
                ctx,
                intent=intent,
                step=int(loop_fields.get("loop_step") or 0),
                latency=latency,
                ok=err is None and not result.get("error"),
                error=(f"{err}: {internal_err}" if internal_err else err) or str(result.get("error") or ""),
                result_summary=_result_summary(result, list(ctx["meta"].get("sandbox") or [])),
                detail={
                    "action": state.get("action"),
                    "redraft": loop_fields.get("loop_redraft_count"),
                    "tokens": meter_tokens,
                    "tier": loop_fields.get("loop_tier"),
                    **({"error_code": err} if err else {}),
                },
                fallback_model=model,
                reply=_last_reply(result),
            )
        # Node wins on redraft/pending; wrapper supplies step/budget defaults
        out = {**loop_fields, **result}
        for key in ("loop_step", "loop_tokens"):
            out[key] = max(int(loop_fields.get(key) or 0), int(result.get(key) or 0))
        if "loop_tokens" in result:
            out["loop_cost_units"] = max(
                float(loop_fields.get("loop_cost_units") or 0.0),
                float(result.get("loop_cost_units") or 0.0),
            )
        if err and not out.get("error"):
            out["error"] = err
        if human:
            return out

        node_err = err or str(result.get("error") or "")
        risky = runaway.is_external_task(tool_name, state.get("action")) and runaway.has_risk_signal(
            node_err, result.get("messages")
        )
        entry = runaway.make_entry(
            tool_name,
            runaway.node_args(state),
            error="" if node_err.startswith(STOP_PREFIXES) else node_err,
            progress=runaway.node_progress(state, result),
            risk=risky,
        )
        history = runaway.push(result.get("loop_history", st.get("loop_history")), entry)
        out["loop_history"] = history
        hit = runaway.detect(history)
        merged = {**st, **out}
        if hit and not is_stopped(out):
            kind, detail = hit
            _trace(merged, int(out.get("loop_step") or 0), "loop_runaway", {"kind": kind, **detail}, ok=False)
            out.update(
                {
                    "error": f"loop_runaway:{kind}",
                    "loop_stop_reason": f"runaway:{kind}",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": progress_report(merged, None, headline=runaway.runaway_text(kind)),
                        }
                    ],
                }
            )
            return out
        if not is_stopped(out):
            _, _, esc = check_budget_or_escalate(merged, where=f"after:{tool_name}")
            if esc:
                out.update(esc)
                merged.update(esc)
            if near_limit(merged):
                out["loop_wrap_up"] = True
        return out

    wrapped.__name__ = getattr(fn, "__name__", tool_name)
    wrapped.__qualname__ = getattr(fn, "__qualname__", tool_name)
    return wrapped
