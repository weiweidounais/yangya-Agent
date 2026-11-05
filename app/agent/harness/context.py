"""Per-step trace context: LLM calls made inside a node are attached to its trace row."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator, Optional

_current: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "harness_step_ctx", default=None
)

_TIER_RANK = {"": 0, "none": 0, "fast": 1, "strong": 2}


@contextmanager
def step_context(**meta: Any) -> Iterator[dict[str, Any]]:
    ctx: dict[str, Any] = {"meta": dict(meta), "llm_calls": [], "guardrails": []}
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


def current() -> Optional[dict[str, Any]]:
    return _current.get()


def record_llm_call(call: dict[str, Any]) -> None:
    ctx = _current.get()
    if ctx is not None:
        ctx["llm_calls"].append(call)


def record_guardrail(hit: dict[str, Any]) -> None:
    ctx = _current.get()
    if ctx is not None:
        ctx["guardrails"].append(hit)
    try:
        from app.observability.tracing import add_event

        add_event(
            f"guardrail.{hit.get('guardrail') or 'hit'}",
            {k: v for k, v in hit.items() if isinstance(v, (str, int, float, bool))},
        )
    except Exception:
        pass


def summarize_llm_calls(calls: list[dict[str, Any]]) -> dict[str, Any]:
    tier = ""
    model = ""
    tin = tout = 0
    failed = 0
    schema_validation_errors = 0
    usage_in = usage_out = 0
    for c in calls:
        t = str(c.get("tier") or "")
        if _TIER_RANK.get(t, 0) >= _TIER_RANK.get(tier, 0):
            tier = t
            model = str(c.get("model") or model)
        tin += int(c.get("tokens_in_est") or 0)
        tout += int(c.get("tokens_out_est") or 0)
        schema_validation_errors += int(c.get("schema_validation_errors") or 0)
        if c.get("ok") is False:
            failed += 1
        usage = c.get("usage") if isinstance(c.get("usage"), dict) else {}
        usage_in += int(usage.get("prompt_tokens") or 0)
        usage_out += int(usage.get("completion_tokens") or 0)
    return {
        "tier": tier,
        "model": model,
        "tokens_in_est": tin,
        "tokens_out_est": tout,
        "llm_calls": len(calls),
        "llm_failed": failed,
        "schema_validation_errors": schema_validation_errors,
        "usage_prompt_tokens": usage_in,
        "usage_completion_tokens": usage_out,
    }
