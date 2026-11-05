"""Chain executor: tier → ordered models, with retry, fallback, limits and usage records."""

from __future__ import annotations

import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from app.gateway import limiter, providers, usage
from app.gateway.config import GatewayConfig, ModelSpec, get_config

_now = time.monotonic
_sleep = time.sleep


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _float_env(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(_env(name) or default))
    except ValueError:
        return default


def _truthy(name: str, default: str) -> bool:
    return (_env(name) or default) not in ("0", "false", "False", "no")


# Legacy defaults: chat_text used 20s/45s, chat_json 25s/60s.
_DEFAULT_TIMEOUTS = {
    ("text", "fast"): 20.0,
    ("text", "strong"): 45.0,
    ("json", "fast"): 25.0,
    ("json", "strong"): 60.0,
}


def attempt_timeout(tier: str, kind: str, spec: Optional[ModelSpec], override: Optional[float]) -> float:
    if override is not None:
        return float(override)
    if spec is not None and spec.timeout:
        return float(spec.timeout)
    env = _env(f"LLM_{tier.upper()}_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return _DEFAULT_TIMEOUTS[(kind, tier)]


@dataclass
class Result:
    content: str = ""
    logprobs: Optional[dict[str, Any]] = None
    spec: Optional[ModelSpec] = None
    tier: str = "fast"
    mode: str = "live"
    request_id: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    last_error: str = ""
    last_error_type: str = ""
    saw_empty: bool = False
    escalated: bool = False
    chain: list[ModelSpec] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.content)


def _backoff(attempt: int, err: providers.ProviderError) -> float:
    base = 0.4 * (2 ** attempt)
    if err.retry_after is not None:
        base = max(base, min(err.retry_after, _float_env("LLM_RETRY_AFTER_MAX", 5.0)))
    return base + random.uniform(0, 0.2)


def _run_chain(
    res: Result,
    cfg: GatewayConfig,
    chain: list[ModelSpec],
    *,
    tier: str,
    task: str,
    kind: str,
    system: str,
    user: str,
    timeout: Optional[float],
    temperature: Optional[float],
    max_tokens: Optional[int],
    logprobs: bool = False,
    response_model: Optional[type[BaseModel]] = None,
) -> None:
    max_retries = _int_env("LLM_MAX_RETRIES", 2)
    max_retries_429 = max(max_retries, _int_env("LLM_MAX_RETRIES_RATE_LIMITED", 4))
    retry_on_timeout = _truthy("LLM_RETRY_ON_TIMEOUT", "0")
    queue_wait = _float_env("LLM_QUEUE_TIMEOUT", 10.0)
    factor = _float_env("LLM_DEADLINE_FACTOR", 2.0)
    first_to = attempt_timeout(tier, kind, chain[0] if chain else None, timeout)
    deadline = _now() + max(first_to, first_to * factor)

    for spec in chain:
        model_system = system
        schema_failures = 0
        api_key = spec.api_key()
        if not api_key:
            continue
        group_cfg = cfg.groups.get(spec.limit_group) or {}
        b = limiter.bucket(
            spec.group(),
            spec.max_concurrency if spec.max_concurrency is not None else group_cfg.get("max_concurrency"),
            spec.rpm if spec.rpm is not None else group_cfg.get("rpm"),
        )
        per_to = attempt_timeout(tier, kind, spec, timeout)
        attempt = -1
        while True:
            attempt += 1
            remaining = deadline - _now()
            if remaining < 0.5:
                res.last_error = "deadline"
                return
            to = min(per_to, remaining)
            t0 = _now()
            err: Optional[providers.ProviderError] = None
            comp: Optional[providers.Completion] = None
            queue_ms = 0.0
            try:
                with b.slot(min(queue_wait, remaining)):
                    queue_ms = (_now() - t0) * 1000
                    to = min(to, max(0.5, deadline - _now()))
                    comp = providers.call(
                        spec,
                        api_key=api_key,
                        system=model_system,
                        user=user,
                        tier=tier,
                        timeout=to,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **({"logprobs": True} if logprobs else {}),
                    )
            except limiter.Busy as e:
                err = providers.ProviderError("busy", str(e))
                queue_ms = (_now() - t0) * 1000
            except providers.ProviderError as e:
                err = e
                res.last_error_type = type(e.__cause__).__name__ if e.__cause__ else e.kind
            except Exception as e:  # noqa: BLE001
                res.last_error_type = type(e).__name__
                err_kind = "timeout" if isinstance(e, TimeoutError) else "connect" if isinstance(e, ConnectionError) else "error"
                err = providers.ProviderError(err_kind, res.last_error_type)
            latency_ms = (_now() - t0) * 1000 - queue_ms
            if err is None or err.kind != "busy":
                b.feedback(ok=err is None, rate_limited=err is not None and err.kind == "rate_limited")

            if err is None and comp is not None and not comp.content:
                err = providers.ProviderError("empty", comp.finish_reason)
                res.saw_empty = True
            if comp is not None and response_model is not None and (err is None or err.kind == "empty"):
                try:
                    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", comp.content.strip(), flags=re.I)
                    response_model.model_validate_json(content)
                except ValidationError as exc:
                    schema_failures += 1
                    # Report schema locations/types only, never private input values.
                    issues = "; ".join(f"{'.'.join(map(str, e['loc'])) or '$'}: {e['type']}"
                                       for e in exc.errors(include_input=False, include_url=False))
                    model_system = system + "\n上次输出未通过格式校验：" + issues + "。请严格遵循 Schema 重新输出完整 JSON。"
                    err = providers.ProviderError("schema", "invalid structured output")
            rec = usage.record(
                request_id=res.request_id,
                task=task,
                tier=tier,
                spec=spec,
                attempt=attempt,
                ok=err is None,
                latency_ms=latency_ms,
                error=err.kind if err else "",
                status=err.status if err else None,
                prompt_tokens=comp.prompt_tokens if comp else None,
                completion_tokens=comp.completion_tokens if comp else None,
                queue_ms=queue_ms,
            )
            res.attempts.append(rec)
            if err is None and comp is not None:
                res.logprobs = comp.logprobs
                res.content = comp.content
                res.spec = spec
                return
            assert err is not None
            from app.agent.harness.redact import redact_text

            res.last_error = redact_text(f"{spec.id}:{err}", max_len=200)
            if err.kind == "schema":
                if schema_failures < 2:
                    continue
                break
            retry = err.retryable or err.kind == "error" or (retry_on_timeout and err.kind == "timeout")
            budget = max_retries_429 if err.kind == "rate_limited" else max_retries
            if not retry or attempt >= budget:
                break
            pause = _backoff(attempt, err)
            if _now() + pause >= deadline - 0.5:
                break
            _sleep(pause)


def complete(
    system: str,
    user: str,
    *,
    tier: str,
    task: str = "",
    kind: str = "text",
    timeout: Optional[float] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    logprobs: bool = False,
    response_model: Optional[type[BaseModel]] = None,
) -> Result:
    cfg = get_config()
    chain = cfg.task_chain(task, tier)
    res = Result(tier=tier, request_id=uuid.uuid4().hex[:12], chain=chain)
    if not any(s.has_key() for s in chain):
        res.mode = "mock"
        res.spec = chain[0] if chain else None
        return res
    kwargs = dict(
        task=task, kind=kind, system=system, user=user,
        timeout=timeout, temperature=temperature, max_tokens=max_tokens, logprobs=logprobs,
        response_model=response_model,
    )
    _run_chain(res, cfg, chain, tier=tier, **kwargs)
    if (
        not res.ok
        and not logprobs
        and tier == "fast"
        and res.saw_empty
        and _truthy("LLM_EMPTY_ESCALATE", "1")
    ):
        tried = {a.get("model_id") for a in res.attempts} if response_model is not None else set()
        strong = [s for s in cfg.chain("strong") if s.has_key() and s.id not in tried]
        if strong:
            res.escalated = True
            kwargs["timeout"] = None
            _run_chain(res, cfg, strong, tier="strong", **kwargs)
    return res
