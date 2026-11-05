"""Central LLM gateway: any vendor via OpenAI-compatible or Anthropic APIs.

All model calls should go through this module (or the thin re-exports in
``app.llm``). Routing is task → tier → ordered model chain:

- FAST  — low latency (chat, classifiers, summaries)
- STRONG — quality (long drafts, hard reasoning)

Each tier is an ordered list of models (``app.gateway.config``); on error,
timeout, rate limit or empty output the next model is tried. Usage per call
is recorded in ``app.gateway.usage``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError

from app.gateway import core, limiter, providers, usage
from app.gateway.config import get_config

from app.agent import token_meter
from app.agent.budget_tiers import classify_difficulty, model_tier_for

from app.observability.tracing import observe
from app.llm import (
    ModelTier,
    _mock_text,
    capabilities,
    has_llm as _legacy_has_llm,
    begin_call_meta,
    end_call_meta,
    _meta,
    model_for,
    resolve_tier,
    routing_config,
)

# Test seam (golden mock mode): fn(kind, system, user, tier, fallback) -> str | dict.
# Only set programmatically by test harnesses; there is no env switch.
LLMOverride = Callable[[str, str, str, ModelTier, Any], Any]
_override: Optional[LLMOverride] = None


def set_llm_override(fn: Optional[LLMOverride]) -> None:
    global _override
    _override = fn


def get_llm_override() -> Optional[LLMOverride]:
    return _override


def _mask_sandbox(system: str, user: str) -> tuple[str, str]:
    """Privacy-sandbox values of the current user never leave the server."""
    from app.agent.harness import context as trace_ctx
    from app.agent.harness.redact import mask_values

    ctx = trace_ctx.current()
    hidden = list(((ctx or {}).get("meta") or {}).get("sandbox") or [])
    if not hidden:
        return system, user
    return (
        mask_values(system, hidden, "[隐私已隐藏]")[0],
        mask_values(user, hidden, "[隐私已隐藏]")[0],
    )


def _trace_call(
    *,
    kind: str,
    task: Optional[str],
    tier: ModelTier,
    system: str,
    user: str,
    output: Any,
    latency_ms: float,
    meta: dict[str, Any],
    error: str = "",
) -> dict[str, Any]:
    """One LLM call on the current step. ``ok`` means the vendor answered and its
    output was used; a mock / fallback answer is ``ok=False`` with a reason."""
    from app.agent.harness import context as trace_ctx
    from app.agent.harness.redact import estimate_tokens, redact_text

    out_meta = output if isinstance(output, dict) else {}
    if _override is not None:
        mode = "override"
        requested = actual = str(out_meta.get("_model") or f"override:{tier.value}")
    else:
        mode = str(meta.get("mode") or out_meta.get("_mode") or ("live" if has_llm(tier) else "mock"))
        requested = str(meta.get("requested_model") or model_for(tier))
        actual = str(meta.get("actual_model") or "")
    fallback_reason = str(meta.get("fallback_reason") or "")
    if not fallback_reason and out_meta.get("_parse"):
        fallback_reason = f"parse:{out_meta.get('_parse')}"
    err = error or str(meta.get("error") or "")
    out_text = output if isinstance(output, str) else str(
        {k: v for k, v in out_meta.items() if not str(k).startswith("_")}
        if isinstance(output, dict)
        else output
    )
    usage = dict(meta.get("usage") or {})
    reported = bool(usage.pop("reported", False))
    call = {
        "kind": kind,
        "task": task or "",
        "tier": tier.value,
        "model": actual or requested,
        "requested_model": requested,
        "actual_model": actual,
        "provider": str(meta.get("provider") or out_meta.get("_provider") or ""),
        "mode": mode,
        "latency_ms": round(latency_ms, 1),
        "attempts": int(meta.get("attempts") or 0),
        "schema_validation_errors": int(meta.get("schema_validation_errors") or 0),
        "attempt_errors": list(meta.get("attempt_errors") or [])[:10],
        "model_fallback": str(meta.get("model_fallback") or ""),
        "usage": usage if reported else None,
        "tokens_in_est": estimate_tokens(system) + estimate_tokens(user),
        "tokens_out_est": estimate_tokens(out_text),
        "ok": not err and not fallback_reason,
        "fallback_reason": fallback_reason,
        "error": redact_text(err, max_len=200),
    }
    trace_ctx.record_llm_call(call)
    return call


def _annotate_generation(gen: Any, call: dict[str, Any], *, system: str, user: str, output: Any) -> None:
    """Langfuse generation attributes: models, usage, cost, fallback, prompt version."""
    from app.observability.cost import cost_for
    from app.observability.prompts import identify, langfuse_link
    from app.prompts import file_version

    usage = call.get("usage") or {}
    reported = bool(usage)
    tin = int(usage.get("prompt_tokens") or 0) if reported else int(call.get("tokens_in_est") or 0)
    tout = int(usage.get("completion_tokens") or 0) if reported else int(call.get("tokens_out_est") or 0)
    model = str(call.get("actual_model") or call.get("requested_model") or "")
    gen.attrs(
        {
            "gen_ai.request.model": str(call.get("requested_model") or ""),
            "gen_ai.response.model": model,
            "langfuse.observation.model.name": model,
            "gen_ai.usage.input_tokens": tin,
            "gen_ai.usage.output_tokens": tout,
            "langfuse.observation.usage_details": {"input": tin, "output": tout, "total": tin + tout},
        }
    )
    cost = cost_for(model, tin, tout) if call.get("mode") == "live" else None
    if cost:
        gen.attr(
            "langfuse.observation.cost_details",
            {"input": cost["input"], "output": cost["output"], "total": cost["total"]},
        )
    prompt = identify(system)
    gen.meta(
        task=call.get("task") or "",
        tier=call.get("tier") or "",
        provider=call.get("provider") or "",
        mode=call.get("mode") or "",
        requested_model=call.get("requested_model") or "",
        actual_model=call.get("actual_model") or "",
        attempts=call.get("attempts") or 0,
        schema_validation_errors=call.get("schema_validation_errors") or 0,
        attempt_errors=call.get("attempt_errors") or [],
        model_fallback=call.get("model_fallback") or "",
        fallback_reason=call.get("fallback_reason") or "",
        usage_source="reported" if reported else "estimated",
        cost_currency=(cost or {}).get("currency"),
        prompt_name=prompt[0] if prompt else "",
        prompt_version=prompt[1] if prompt else "",
        prompt_file_version=file_version(*prompt) if prompt else "",
        latency_ms=call.get("latency_ms"),
    )
    if prompt:
        link = langfuse_link(*prompt)
        if link:
            gen.attr("langfuse.observation.prompt.name", link["langfuse_name"])
            gen.attr("langfuse.observation.prompt.version", int(link["version"]))
    gen.input({"system_prompt": f"{prompt[0]}@{prompt[1]}" if prompt else system[:300], "user": user})
    if isinstance(output, dict):
        output = {k: v for k, v in output.items() if not str(k).startswith("_")}
    gen.output(output)
    if call.get("error"):
        gen.level("ERROR", str(call["error"]))
    elif call.get("fallback_reason") or call.get("ok") is False:
        gen.level("WARNING", f"fallback: {call.get('fallback_reason') or call.get('mode')}")


def _finish_call(
    gen: Any,
    kind: str,
    task: Optional[str],
    tier: ModelTier,
    system: str,
    user: str,
    out: Any,
    t0: float,
    meta: dict[str, Any],
    err: str,
) -> None:
    try:
        call = _trace_call(
            kind=kind,
            task=task,
            tier=tier,
            system=system,
            user=user,
            output=out,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            meta=meta,
            error=err,
        )
        if gen.recording:
            try:
                _annotate_generation(gen, call, system=system, user=user, output=out)
            except Exception:
                pass
    finally:
        end_call_meta()


def route(task: str, **_: Any) -> ModelTier:
    """Map a logical task to FAST/STRONG: explicit gateway config first, else difficulty tiers."""
    t = (task or "").strip().lower()
    tier = get_config().tasks.get(t)
    if tier is None:
        return model_tier_for(classify_difficulty(task))
    return ModelTier.STRONG if tier == "strong" else ModelTier.FAST


def has_llm(tier: Optional[ModelTier] = None, task: str = "") -> bool:
    if _override is not None:
        return True
    cfg = get_config()
    if task:
        return any(s.has_key() for s in cfg.task_chain(task, (tier or route(task)).value))
    tiers = [tier.value] if tier is not None else ["fast", "strong"]
    if any(s.has_key() for t in tiers for s in cfg.chain(t)):
        return True
    return _legacy_has_llm(tier)


def _task_name(task: Optional[str], tier: ModelTier) -> str:
    return (task or "").strip().lower() or f"tier:{tier.value}"


def _meter(system: str, user: str, out: Any, task: str) -> None:
    try:
        rendered = json.dumps(out, ensure_ascii=False) if isinstance(out, dict) else str(out)
    except (TypeError, ValueError):
        rendered = str(out)
    token_meter.record_completion(system, user, rendered, task=task)


def _record_result_meta(res: core.Result, tier: ModelTier) -> None:
    """Feed the harness from the unified executor, including every attempt."""
    first = res.chain[0] if res.chain else None
    attempts = res.attempts
    actual = res.spec.model if res.spec and res.ok else ""
    requested = first.model if first else model_for(tier)
    prompt = sum(int(a.get("prompt_tokens") or 0) for a in attempts)
    completion = sum(int(a.get("completion_tokens") or 0) for a in attempts)
    error = ""
    reason = ""
    if res.mode == "mock":
        reason = "no_api_key"
    elif not res.ok:
        error = "empty_output" if res.saw_empty else res.last_error
        reason = "empty_content" if res.saw_empty else f"provider_error:{res.last_error_type or res.last_error}"
    _meta().update(
        requested_model=requested,
        actual_model=actual,
        provider=res.spec.vendor if res.spec else (first.vendor if first else ""),
        mode=res.mode if res.ok or res.mode == "mock" else "mock_fallback",
        attempts=len(attempts),
        attempt_errors=[{"model": a.get("model", ""), "error": a.get("error", "")}
                        for a in attempts if not a.get("ok")],
        usage={"prompt_tokens": prompt, "completion_tokens": completion,
               "total_tokens": prompt + completion,
               "reported": any(a.get("prompt_tokens") is not None or
                               a.get("completion_tokens") is not None for a in attempts)},
        model_fallback=("empty_content:" if res.escalated else "") + f"{requested}->{actual}"
        if actual and (actual != requested or res.escalated) else "",
        fallback_reason=reason,
        error=error,
    )


# Shown when keys are configured but every model in the chain failed. The
# "[mock-llm]" placeholder is only for the no-key offline mode.
UPSTREAM_ERROR_TEXT = "模型服务暂时不可用，请稍后再试。"

class OutputSchemaError(ValueError):
    """The model failed its output contract after one repair retry."""


def _complete_json(system, user, *, tier, task, timeout, max_tokens, fallback, response_model=None):
    res = core.complete(
        system + "\n只输出合法 JSON，不要 markdown 围栏。回答尽量短。",
        user,
        tier=tier.value,
        task=_task_name(task, tier),
        kind="json",
        timeout=timeout,
        temperature=0.3,
        max_tokens=max_tokens,
        **({"response_model": response_model} if task == "profile_parse" else {}),
    )
    _record_result_meta(res, tier)
    _meta()["schema_validation_errors"] = _meta().get("schema_validation_errors", 0) + sum(
        a.get("error") == "schema" for a in res.attempts
    )
    first = res.chain[0] if res.chain else None
    meta_model = (res.spec or first).model if (res.spec or first) else model_for(tier)
    meta_vendor = (res.spec or first).vendor if (res.spec or first) else ""
    if res.mode == "mock":
        out: Any = fallback if fallback is not None else {"text": _mock_text(user)}
        if isinstance(out, dict):
            out = dict(out)
            out["_tier"] = tier.value
            out["_mode"] = "mock"
            out["_provider"] = meta_vendor
            out["_model"] = meta_model
        return out

    base_meta = {"_model": meta_model, "_tier": tier.value, "_provider": meta_vendor}
    if task == "profile_parse" and not res.ok:
        raise OutputSchemaError("profile_parse: model chain exhausted without valid structured output")
    if not res.ok and not res.saw_empty:
        if fallback is not None:
            out = dict(fallback)
            out["_model_error"] = res.last_error[:200]
            out["_tier"] = tier.value
            out["_provider"] = meta_vendor
            out["_mode"] = "mock_fallback"
            return out
        return {"text": UPSTREAM_ERROR_TEXT, "error": res.last_error, "_tier": tier.value, "_provider": meta_vendor}

    content = re.sub(r"^```(?:json)?\s*", "", res.content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content)
    if response_model is not None:
        # Remove presentation fences, then validate before metadata or fallbacks.
        data = response_model.model_validate_json(content).model_dump()
        return {**data, **base_meta}
    if not content:
        _meta().update(error="empty_output", fallback_reason="empty_content")
        if fallback is not None:
            return {**fallback, **base_meta, "_parse": "empty_content"}
        return {"raw": "", **base_meta, "_parse": "empty_content"}
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        _meta().update(error="bad_json", fallback_reason="json_parse_error")
        if fallback is not None:
            return {**fallback, **base_meta, "_parse": "fallback"}
        return {"raw": content, **base_meta}
    if isinstance(data, dict):
        data.update(base_meta)
    return data


def chat_json(
    system: str,
    user: str,
    *,
    tier: ModelTier = ModelTier.FAST,
    fallback: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
    task: Optional[str] = None,
    max_tokens: Optional[int] = None,
    response_model: Optional[type[BaseModel]] = None,
) -> dict[str, Any]:
    """Gateway JSON completion. Optional ``task`` overrides tier via route()."""
    if task:
        tier = route(task)
    system, user = _mask_sandbox(system, user)
    with observe(f"llm.{task or 'json'}", kind="generation") as gen:
        return _run_json(gen, system, user, tier=tier, fallback=fallback, timeout=timeout, task=task, max_tokens=max_tokens, response_model=response_model)


def _run_json(
    gen: Any,
    system: str,
    user: str,
    *,
    tier: ModelTier,
    fallback: Optional[dict[str, Any]],
    timeout: Optional[float],
    task: Optional[str],
    max_tokens: Optional[int] = None,
    response_model: Optional[type[BaseModel]] = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    err = ""
    out: Any = None
    meta = begin_call_meta()
    try:
        for attempt in range(2 if response_model is not None else 1):
            try:
                if _override is not None:
                    out = _override("json", system, user, tier, fallback)
                    if response_model is not None:
                        out = response_model.model_validate(out).model_dump()
                    else:
                        out = dict(out) if isinstance(out, dict) else dict(fallback or {})
                    out.setdefault("_model", f"override:{tier.value}")
                    out.setdefault("_tier", tier.value)
                    out.setdefault("_mode", "override")
                else:
                    out = _complete_json(
                        system, user, tier=tier, task=task, timeout=timeout,
                        max_tokens=max_tokens, fallback=fallback, response_model=response_model,
                    )
                break
            except ValidationError as exc:
                if response_model is None:
                    raise
                meta["schema_validation_errors"] = meta.get("schema_validation_errors", 0) + 1
                # Do not echo private user text from validation inputs.
                issues = "; ".join(
                    f"{'.'.join(map(str, e['loc'])) or '$'}: {e['type']}"
                    for e in exc.errors(include_input=False, include_url=False)
                )
                if attempt == 1:
                    raise OutputSchemaError(
                        f"{response_model.__name__}: invalid JSON output after one retry ({issues})"
                    ) from None
                system += (
                    "\n上次输出未通过格式校验：" + issues
                    + "。请重新生成完整 JSON，严格遵循上述 Schema；不要添加解释或围栏。"
                )
        _meter(system, user, out, task or tier.value)
        return out
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _finish_call(gen, "json", task, tier, system, user, out, t0, meta, err)


def chat_text(
    system: str,
    user: str,
    *,
    tier: ModelTier = ModelTier.FAST,
    fallback: str = "",
    timeout: Optional[float] = None,
    task: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> str:
    """Gateway text completion. Optional ``task`` overrides tier via route()."""
    if task:
        tier = route(task)
    system, user = _mask_sandbox(system, user)
    with observe(f"llm.{task or 'text'}", kind="generation") as gen:
        return _run_text(gen, system, user, tier=tier, fallback=fallback, timeout=timeout, task=task, max_tokens=max_tokens)


def _run_text(
    gen: Any,
    system: str,
    user: str,
    *,
    tier: ModelTier,
    fallback: str,
    timeout: Optional[float],
    task: Optional[str],
    max_tokens: Optional[int] = None,
) -> str:
    t0 = time.perf_counter()
    err = ""
    out: Any = None
    meta = begin_call_meta()
    try:
        if _override is not None:
            res = _override("text", system, user, tier, fallback)
            out = res if isinstance(res, str) else (fallback or "")
        else:
            res = core.complete(
                system + "\n回答简洁，不要废话。", user,
                tier=tier.value, task=_task_name(task, tier), kind="text",
                timeout=timeout, temperature=0.4, max_tokens=max_tokens,
            )
            _record_result_meta(res, tier)
            if res.ok:
                out = res.content
            else:
                out = fallback or (_mock_text(user) if res.mode == "mock" else UPSTREAM_ERROR_TEXT)
        _meter(system, user, out, task or tier.value)
        return out
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _finish_call(gen, "text", task, tier, system, user, out, t0, meta, err)


def complete(
    *,
    system: str,
    user: str,
    task: str = "chat",
    as_json: bool = False,
    fallback: Any = None,
    timeout: Optional[float] = None,
) -> Any:
    """Single entry for text/json completions via the gateway."""
    tier = route(task)
    if as_json:
        return chat_json(
            system,
            user,
            tier=tier,
            fallback=fallback if isinstance(fallback, dict) or fallback is None else None,
            timeout=timeout,
            task=task,
        )
    return chat_text(
        system,
        user,
        tier=tier,
        fallback=fallback if isinstance(fallback, str) else (fallback or ""),
        timeout=timeout,
        task=task,
    )


def usage_snapshot(limit: int = 50) -> dict[str, Any]:
    return {
        "totals": usage.totals(),
        "recent": usage.recent(limit),
        "limits": limiter.stats(),
        "http_pool": providers.pool_stats(),
    }

def _observability_info() -> dict[str, Any]:
    try:
        from app.observability.tracing import status

        st = status()
        return {
            "langfuse": st["active"],
            "export_io": st["export_io"],
            "environment": st["environment"],
            "reason": st["reason"] or st["error"],
        }
    except Exception as exc:
        return {"langfuse": False, "reason": type(exc).__name__}


def gateway_info() -> dict[str, Any]:
    """Snapshot for /health and /api/capabilities."""
    cfg = routing_config()
    return {
        "gateway": "app.gateway.llm_gateway",
        "any_vendor": True,
        "tiers": ["fast", "strong"],
        "routing": cfg,
        "models": get_config().public(),
        "usage": usage.public_snapshot(0)["totals"],
        "limits": limiter.stats(),
        "channels": ["web", "feishu", "openclaw_wechat"],
        "confirm_policy": "no_expiry_later_overrides",
        "orchestration": "langgraph_router_subgraphs_loop",
        "memory": {
            "working": "langgraph_checkpoint",
            "profile": "sqlite",
            "episodic": "sqlite_keyword_tag",
            "dialogue_summary": True,
            "subgraphs": True,
            "vectors": False,
        },
        "loop": {
            "budgets": True,
            "budget_dimensions": ["steps", "tokens_estimate", "cost_estimate", "wall_clock"],
            "budget_tiers": ["simple", "medium", "hard"],
            "tier_escalation": "once_per_task",
            "cycle": "observe_plan_tool_validate",
            "retry_with_reason": True,
            "reject_redraft": True,
            "gateway_backoff": True,
            "empty_escalate_fast_to_strong": True,
            "model_chain_fallback": True,
            "protocols": ["openai", "anthropic"],
        },
        "harness": {
            "traces": True,
            "trace_fields": [
                "input_summary",
                "tool",
                "params",
                "result_summary",
                "latency_ms",
                "tier",
                "model",
                "tokens_in_est",
                "tokens_out_est",
                "llm_calls",
                "guardrails",
            ],
            "llm_call_fields": [
                "requested_model",
                "actual_model",
                "attempts",
                "usage",
                "ok",
                "fallback_reason",
                "error",
            ],
            "trace_writer": "async_batched",
            "trace_ids": "hmac_pseudonyms",
            "trace_query": "GET /api/admin/traces (per-admin tokens, audited)",
            "admin_audit": "GET /api/admin/audit",
            "feedback": "POST /api/feedback -> Langfuse score user_feedback",
            "guardrails": [
                "no_leak_secrets",
                "no_leak_system_prompt",
                "no_leak_other_users",
                "no_leak_privacy_sandbox",
                "group_no_personal_data",
                "confirm_before_draft_use",
            ],
            "golden": "tests/golden/run_golden.py",
            "golden_gate": "safety zero-fail + quality >= baseline",
            "golden_nightly": "live --repeat 3, safety pass^3",
            "judge_rubrics": ["empathy", "crisis_handling"],
        },
        "observability": _observability_info(),
        "vectors": False,
        "agent_package": "app.agent",
    }


__all__ = [
    "ModelTier",
    "capabilities",
    "chat_json",
    "chat_text",
    "complete",
    "gateway_info",
    "get_llm_override",
    "has_llm",
    "model_for",
    "resolve_tier",
    "route",
    "routing_config",
    "usage_snapshot",
    "set_llm_override",
]


def _mask_intent_input(text: str) -> str:
    """Routing can run before a graph step; use trusted identity for its sandbox."""
    from app.data_access import IdentityRequired, current_user
    from app.agent.harness.leak import sandbox_values
    from app.agent.harness.redact import mask_values

    try:
        uid = current_user()
    except IdentityRequired:
        uid = None
    hidden = sandbox_values(uid or "", strict=True)
    return _mask_sandbox("", mask_values(text, hidden, "[隐私已隐藏]")[0])[1]


def embed_texts(texts: list[str], *, model: str, base_url: str) -> list[list[float]]:
    """Hosted intent embeddings; never cache user input or expose provider errors."""
    import os
    import httpx

    key = os.environ["SILICONFLOW_API_KEY"]
    if not key.strip():
        raise ValueError("missing embedding credential")
    inputs = [_mask_intent_input(text) for text in texts]
    try:
        response = httpx.post(
            base_url + "/embeddings",
            headers={"Authorization": "Bearer " + key},
            json={"model": model, "input": inputs, "encoding_format": "float"},
            timeout=30,
        )
        response.raise_for_status()
        batch = sorted(response.json()["data"], key=lambda row: row["index"])
        if [row["index"] for row in batch] != list(range(len(inputs))):
            raise ValueError("invalid embedding indexes")
        return [row["embedding"] for row in batch]
    except Exception:
        raise RuntimeError("SiliconFlow embedding request failed") from None


def classify_label(text: str, labels: tuple[str, ...]) -> Optional[tuple[str, float]]:
    """FAST-tier second opinion; missing/invalid token logprobs fail closed.

    Use absolute emitted-token probability, never renormalize away probability
    mass outside the candidate labels. No self-reported confidence is accepted.
    """
    import math
    mapping = {chr(65 + i): label for i, label in enumerate(labels)}
    system = "判断用户意图，只输出一个字母。含糊、否定执行或多个意图选unresolved。标签：" + json.dumps(mapping, ensure_ascii=False)
    system, user = _mask_sandbox(system, _mask_intent_input(text))
    tier = ModelTier.FAST
    t0 = time.perf_counter()
    out = None
    meta = begin_call_meta()
    with observe("llm.intent_second_opinion", kind="generation") as gen:
        try:
            if _override is not None:
                return None  # ordinary mock text is never logprob evidence
            res = core.complete(system, user, tier="fast", task="intent_second_opinion",
                                kind="text", timeout=3., temperature=0., max_tokens=1, logprobs=True)
            _record_result_meta(res, tier)
            content = (res.logprobs or {}).get("content") or []
            if not res.ok or len(content) != 1:
                return None
            token = content[0]["token"]
            logprob = float(content[0]["logprob"])
            if token not in mapping or res.content != token or not math.isfinite(logprob) or logprob > 0:
                return None
            out = (mapping[token], math.exp(logprob))
            _meter(system, user, token, "intent_second_opinion")
            return out
        except (KeyError, TypeError, ValueError):
            return None
        finally:
            _finish_call(gen, "logprobs", "intent_second_opinion", tier, system, user, out, t0, meta, "")
