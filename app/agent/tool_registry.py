"""Enforced tool boundary shared by Python callers and schema consumers.

Context is trusted server state, never model-supplied arguments. Decorated Python
calls retain their return values/exceptions; invoke_tool returns structured gate
errors. Execution workers inherit trusted authorization and trace context.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager, nullcontext
import functools
import importlib
import inspect
import logging
import os
import random
import threading
import uuid
import httpx
from datetime import datetime
from collections.abc import Callable as ABCCallable
import time
import types
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union, get_args, get_origin, get_type_hints

from jsonschema import Draft202012Validator, ValidationError, validators


logger = logging.getLogger(__name__)


class ToolError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail

    def as_dict(self):
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.detail}}


class ToolStatusUnknown(ToolError):
    """An effect may have happened; never encourage automatic resubmission."""
    def __init__(self, operation_id):
        super().__init__("status_unknown", "结果未确认，请到平台上查看，不要重复操作", operation_id=operation_id)

    def as_dict(self):
        return {"ok": None, "status": "unknown",
                "message": self.message, **self.detail}


def _python_type(validator, expected, value, schema):
    from sqlalchemy.orm import Session
    from app.agent.proactive.config import ProactiveConfig
    types = {"datetime": datetime, "session": Session, "config": ProactiveConfig}
    valid = callable(value) if expected == "callable" else isinstance(value, types[expected])
    if not valid:
        yield ValidationError("Invalid server runtime dependency")


ParameterValidator = validators.extend(Draft202012Validator, {"x-python-type": _python_type})


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    when_to_use: str
    when_not_to_use: str
    params_schema: dict
    return_schema: dict
    risk_level: str = "read"
    requires_confirm: bool = False
    timeout: float = 120.0
    max_retries: int = 0
    status: str = "live"
    sensitive: bool = False
    tags: list[str] = field(default_factory=list)
    identity_params: tuple[str, ...] = ()


_REGISTRY: dict[str, ToolSpec] = {}
_DECLARED: dict[str, ToolSpec] = {}
_BOOTSTRAPPED = False
_CONTEXT = contextvars.ContextVar("tool_context", default=None)
_CONFIRM = contextvars.ContextVar("tool_confirm", default=None)
_TRACE_ENABLED = contextvars.ContextVar("tool_trace_enabled", default=True)
_AUDIT_LOOKUP = contextvars.ContextVar("tool_audit_lookup", default=False)


@contextmanager
def audit_lookup():
    """Do not recursively audit the trace writer's own redaction profile lookup.

    Validation and authorization still run. This is infrastructure, not a new
    user tool invocation; its result only supplies the trace's redaction values.
    """
    token = _AUDIT_LOOKUP.set(True)
    try:
        yield
    finally:
        _AUDIT_LOOKUP.reset(token)


def _schema(annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, types.UnionType):
        return {"anyOf": [_schema(a) for a in args]}
    kind = origin or annotation
    if kind in (str, int, float, bool, dict, list, tuple, type(None)):
        out = {"type": {str: "string", int: "integer", float: "number", bool: "boolean",
                        dict: "object", list: "array", tuple: "array", type(None): "null"}[kind]}
        if kind is list and args:
            out["items"] = _schema(args[0])
        return out
    if kind is datetime:
        return {"x-python-type": "datetime"}
    if kind is ABCCallable:
        return {"x-python-type": "callable"}
    runtime = {"Session": "session", "ProactiveConfig": "config"}
    if getattr(kind, "__name__", "") in runtime:
        return {"x-python-type": runtime[kind.__name__]}
    # Any JSON value, rather than an undocumented empty schema.
    return {"type": ["object", "array", "string", "number", "boolean", "null"]}


def _schemas(fn):
    try:
        hints = get_type_hints(fn)
    except (NameError, TypeError):
        hints = {}
    props, required = {}, []
    for key, p in inspect.signature(fn).parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        overrides = {
            "mutate": {"x-python-type": "callable"},
            "card": {"type": ["object", "null"]},
        }
        props[key] = overrides.get(key, _schema(hints.get(key, Any)))
        if p.default is p.empty:
            required.append(key)
    return ({"type": "object", "properties": props, "required": required,
             "additionalProperties": False}, _schema(hints.get("return", Any)))


def register(name, fn, *, description="", sensitive=False, tags=None,
             when_to_use="", when_not_to_use="", params_schema=None, return_schema=None,
             risk_level="read", requires_confirm=False, timeout=120.0, max_retries=0, status="live"):
    if risk_level not in {"read", "self_write", "notify_self", "external"}:
        raise ValueError("invalid risk level")
    if requires_confirm and risk_level != "external":
        raise ValueError("requires_confirm is only supported for external tools")
    if status not in {"live", "dry_run", "not_connected"} or timeout <= 0 or max_retries < 0:
        raise ValueError("invalid tool execution policy")
    if name.startswith("proactive.") and risk_level != "notify_self":
        raise ValueError("proactive jobs must declare notify_self")
    from app.agent.tool_catalog import policy
    reviewed = policy(name, risk_level)
    if reviewed:
        description = reviewed["description"]
        when_to_use = reviewed["when_to_use"]
        when_not_to_use = reviewed["when_not_to_use"]
        timeout = reviewed["timeout"]
        max_retries = reviewed["max_retries"]
    if risk_level != "read":
        max_retries = 0
    params, returns = _schemas(fn)
    params = params if params_schema is None else params_schema
    params = {**params, "properties": {k: v for k, v in params.get("properties", {}).items()
                                       if k not in {"user_id", "owner_id"}},
              "required": [k for k in params.get("required", []) if k not in {"user_id", "owner_id"}]}
    returns = returns if return_schema is None else return_schema
    Draft202012Validator.check_schema(params)
    Draft202012Validator.check_schema(returns)
    _REGISTRY[name] = ToolSpec(
        name, description or name, fn, when_to_use or description or name,
        when_not_to_use or "Do not use outside the user's requested scope or for another user's data.",
        params, returns, risk_level, risk_level == "external",
        timeout, max_retries, status, sensitive, list(tags or []),
        tuple(k for k in inspect.signature(fn).parameters if k in {"user_id", "owner_id"}))
    return fn


def tool(name, **policy):
    """Route even existing direct imports through invoke_tool, without monkeypatching."""
    def decorate(fn):
        register(name, fn, **policy)
        _DECLARED[name] = _REGISTRY[name]
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            ctx = dict(_CONTEXT.get() or {})
            ctx["raise_errors"] = True
            return invoke_tool(name, dict(bound.arguments), ctx)
        return wrapped
    return decorate


def get(name):
    ensure_defaults()
    if name == "memory_facts_search":
        from app.feature_flags import memory_vector_enabled
        from app.data_access import IdentityRequired
        try:
            enabled = memory_vector_enabled()
        except IdentityRequired:
            enabled = False
        if not enabled:
            return None
    return _REGISTRY.get(name)


def call(name, *args, **kwargs):
    spec = get(name)
    if spec is None:
        logger.warning("Unknown tool: %s", name)
        raise ToolError("unknown_tool", "这个操作暂不支持")
    arguments = dict(inspect.signature(spec.fn).bind(*args, **kwargs).arguments)
    ctx = {"raise_errors": True}
    return invoke_tool(name, arguments, ctx)


def _check_owner(args, ctx):
    owner = ctx.get("user_id")
    if not owner or args.get("user_id", owner) != owner:
        raise ToolError("notify_self_forbidden", "主动提醒只能发送给当前账号本人。")
    for key in ("recipient_id", "target_user_id", "owner_id"):
        if key in args and args[key] != owner:
            raise ToolError("notify_self_forbidden", "主动提醒不能发送给其他人。")
    if "external_user_id" in args or "channel" in args:
        from app.channels.binding import resolve_user_id
        if not args.get("channel") or not args.get("external_user_id") or resolve_user_id(
            args["channel"], args["external_user_id"]
        ) != owner:
            raise ToolError("notify_self_forbidden", "发送渠道未绑定到当前账号本人。")


def _confirm(spec, args, ctx):
    uid = ctx.get("user_id")
    if not uid or args.get("user_id", uid) != uid:
        raise ToolError("confirm_required", "需要当前账号的有效确认凭证。")
    token = ctx.get("confirm_token") or args.get("confirm_token") or ""
    from app.confirm_store import consume_token
    # Bind generic tokens to both tool and exact arguments, not a reusable boolean.
    consume_token(token, confirmation_scope(spec.name, args), uid)
    return uid


def confirmation_scope(name, args):
    import hashlib
    import json
    payload = {k: v for k, v in args.items() if k != "confirm_token"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       separators=(",", ":")).encode()).hexdigest()
    return f"tool:{name}:{digest}"


def current_confirm():
    return _CONFIRM.get()


def _sample_read():
    try:
        rate = float(os.getenv("TOOL_TRACE_READ_SAMPLE", "0.1"))
        if not 0 <= rate <= 1:
            rate = 0.1
    except ValueError:
        rate = 0.1
    return random.random() < rate


_EXECUTION = contextvars.ContextVar("tool_execution", default=None)


def _execute(spec, args, ctx, deadline, operation_id):
    """One worker per invocation tree keeps transactions on one thread.

    Nested tools publish shorter deadlines to the waiting caller. On timeout,
    the whole stack remains together until completion: no concurrent rollback
    races against an in-flight database commit. Expired nested calls cannot start.
    """
    active = _EXECUTION.get()
    if active is not None:
        condition = active["condition"]
        with condition:
            active["deadlines"][operation_id] = deadline
            active["effects"] |= spec.risk_level != "read"
            condition.notify_all()
        try:
            if time.monotonic() >= deadline:
                raise ToolError("timeout", "操作等待超时，尚未开始执行。")
            return spec.fn(**args)
        finally:
            with condition:
                # Keep an exceeded deadline visible until the caller observes it.
                if time.monotonic() < deadline:
                    active["deadlines"].pop(operation_id, None)
                condition.notify_all()

    condition = threading.Condition()
    active = {"condition": condition, "deadlines": {operation_id: deadline},
              "effects": spec.risk_level != "read", "done": False, "abandoned": False}
    copied = contextvars.copy_context()

    def work():
        result, exc = None, None
        execution_token = _EXECUTION.set(active)
        try:
            if time.monotonic() >= deadline:
                raise ToolError("timeout", "操作等待超时，尚未开始执行。")
            result = spec.fn(**args)
        except BaseException as caught:
            exc = caught
        finally:
            _EXECUTION.reset(execution_token)
        with condition:
            active.update(done=True, result=result, exc=exc)
            abandoned = active["abandoned"]
            condition.notify_all()
        if abandoned and active["effects"]:
            from app.agent.harness.traces import record_step
            from app.observability.tools import _shape
            valid = exc is None and Draft202012Validator(spec.return_schema).is_valid(result)
            pending = isinstance(result, dict) and result.get("status") == "unknown"
            record_step(thread_id=ctx.get("thread_id") or "tool-registry",
                        user_id=ctx.get("user_id") or "", intent="tool_registry",
                        tool=spec.name, ok=True if valid and not pending else None,
                        error="" if valid and not pending else "status_unknown",
                        result_summary=str(_shape(result)),
                        detail={"operation_id": operation_id,
                                "status": "completed" if valid and not pending else "unknown"})

    threading.Thread(target=lambda: copied.run(work), daemon=True, name="tool-execution").start()
    with condition:
        while not active["done"]:
            remaining = min(active["deadlines"].values()) - time.monotonic()
            if remaining <= 0:
                active["abandoned"] = True
                if active["effects"]:
                    raise ToolStatusUnknown(operation_id)
                raise ToolError("timeout", "查询超时，请稍后重试。")
            condition.wait(remaining)
        if active["exc"] is not None:
            raise active["exc"]
        return active["result"]


def invoke_tool(name: str, args: dict, ctx: Optional[dict] = None):
    """Validate, gate, execute and audit. Only read tools retry invalid outputs.

    Invalid inputs can be repaired by a trusted ctx['repair_args'] callback with
    model-readable errors, bounded by max_retries. Effects are never replayed.
    Deadlines bound the wait during execution and are inherited by nested tools.
    An in-flight effect is never replayed or reported as a known failure.
    """
    from app.observability import tracing
    from app.observability.tools import _shape
    from app.agent.harness.traces import record_step
    from app.agent.harness.identity import user_pseudonym
    # Trace metadata and execution policy can be inherited; identity is always
    # replaced below with the authenticated context, never caller metadata.
    from app.agent.harness.context import current
    step_meta = (current() or {}).get("meta", {})
    inherited = {key: step_meta[key] for key in ("thread_id",) if step_meta.get(key)}
    inherited.update(_CONTEXT.get() or {})
    inherited.update(ctx or {})
    ctx = inherited
    from app.data_access import current_user, IdentityRequired
    try:
        trusted_uid = current_user()
    except IdentityRequired:
        trusted_uid = None
        has_identity = False
    else:
        has_identity = True
    ctx["user_id"] = trusted_uid
    start, result, error, confirmer = time.monotonic(), None, "", ""
    context_token = confirm_token = None
    spec = get(name)
    operation_id = uuid.uuid4().hex
    started = False
    audit = not _AUDIT_LOOKUP.get() and (spec is None or spec.risk_level != "read" or _sample_read())
    trace_token = _TRACE_ENABLED.set(audit)
    observation = tracing.observe(f"tool.registry.{name}", kind="tool") if audit else nullcontext(tracing.Observation())
    with observation as obs:
        try:
            if name == "memory_facts_search" and trusted_uid is None:
                from app.feature_flags import memory_vector_enabled
                try:
                    memory_vector_enabled()
                except IdentityRequired:
                    raise ToolError("identity_required", "Trusted identity required")
            spec = get(name)
            if spec is None:
                logger.warning("Unknown tool: %s", name)
                raise ToolError("unknown_tool", "这个操作暂不支持")
            if spec.status == "not_connected":
                raise ToolError("not_connected", "暂不支持这项操作，相关服务尚未连接，未执行任何操作。")
            identity_params = spec.identity_params
            private = bool(identity_params) or spec.risk_level != "read" or name == "memory_get"
            if private and (not has_identity or trusted_uid is None):
                raise ToolError("identity_required", "Trusted identity required")
            if isinstance(args, dict):
                args = {k: v for k, v in args.items() if k not in {"user_id", "owner_id"}}
            for attempt in range(spec.max_retries + 1):
                failures = list(ParameterValidator(spec.params_schema).iter_errors(args))
                if not failures:
                    break
                detail = [{"path": list(e.absolute_path), "rule": e.validator} for e in failures]
                repair = ctx.get("repair_args")
                if attempt == spec.max_retries or not callable(repair):
                    logger.warning("Tool arguments do not match JSON Schema: tool=%s errors=%s", name, detail)
                    raise ToolError("schema_validation", "提供的信息不符合要求，请检查后重试。")
                args = repair(args, {"code": "schema_validation", "errors": detail, "attempt": attempt + 1})
            args = {k: v for k, v in args.items() if k not in {"user_id", "owner_id"}}
            args.update({key: trusted_uid for key in identity_params})
            if spec.risk_level == "notify_self":
                _check_owner(args, ctx)
            deadline = min(ctx.get("deadline", float("inf")), start + spec.timeout)
            if time.monotonic() >= deadline:
                logger.warning("Tool deadline exceeded before execution: tool=%s", name)
                raise ToolError("timeout", "操作等待超时，尚未开始执行。")
            ctx["deadline"] = deadline
            context_token = _CONTEXT.set(ctx)
            if spec.risk_level == "external":
                verified = _confirm(spec, args, ctx)
                confirm_token = _CONFIRM.set(verified)
                confirmer = user_pseudonym(ctx["user_id"])
            for attempt in range(spec.max_retries + 1):
                if time.monotonic() >= deadline:
                    raise ToolError("timeout", "操作等待超时，尚未开始执行。")
                started = True
                try:
                    result = _execute(spec, args, ctx, deadline, operation_id)
                except (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError):
                    if spec.risk_level == "read" and attempt < spec.max_retries and time.monotonic() < deadline:
                        continue
                    raise
                if spec.risk_level == "read" and time.monotonic() >= deadline:
                    raise ToolError("timeout", "查询超时，请稍后重试。")
                try:
                    Draft202012Validator(spec.return_schema).validate(result)
                    break
                except ValidationError as exc:
                    if spec.risk_level != "read":
                        raise ToolStatusUnknown(operation_id) from exc
                    if attempt == spec.max_retries:
                        logger.warning("Tool result does not match JSON Schema: tool=%s path=%s rule=%s",
                                       name, list(exc.absolute_path), exc.validator)
                        raise ToolError("return_validation", "返回的结果不符合要求，请稍后重试。") from exc
            if isinstance(result, dict) and result.get("status") == "unknown":
                error = "status_unknown"
                result = {**result, **ToolStatusUnknown(operation_id).as_dict()}
                result.pop("verification", None)
            return result
        except Exception as exc:
            # Typed precondition refusals are known failures; unexpected execution
            # errors may follow a commit or an accepted remote request.
            if (started and spec is not None and spec.risk_level != "read"
                    and not isinstance(exc, ToolStatusUnknown)
                    and not (getattr(exc, "code", "").startswith("confirm_")
                             or isinstance(exc, ToolError) and exc.code != "timeout"
                             or 400 <= getattr(exc, "http_status", 0) < 500
                             and getattr(exc, "http_status", 0) != 408)):
                exc = ToolStatusUnknown(operation_id)
            error = getattr(exc, "code", type(exc).__name__)
            if isinstance(exc, ToolStatusUnknown):
                result = exc.as_dict()
            if ctx.get("raise_errors"):
                raise exc
            result = exc.as_dict() if isinstance(exc, ToolError) else {
                "ok": False, "error": {"code": error, "message": "工具未完成，请检查连接或确认凭证后重试。"}}
            return result
        finally:
            _TRACE_ENABLED.reset(trace_token)
            if confirm_token is not None:
                _CONFIRM.reset(confirm_token)
            if context_token is not None:
                _CONTEXT.reset(context_token)
            latency = (time.monotonic() - start) * 1000
            params = {k: _shape(v) for k, v in args.items()} if isinstance(args, dict) else {"invalid_args": _shape(args)}
            summary = _shape(result)
            obs.input(params).output(summary).meta(tool=name, latency_ms=latency, confirmer=confirmer, error=error)
            if audit:
                record_step(thread_id=ctx.get("thread_id") or "tool-registry", user_id=ctx.get("user_id") or "",
                            intent="tool_registry", tool=name, latency_ms=latency, ok=None if error == "status_unknown" else not bool(error), error=error,
                            params=params, result_summary=str(summary), detail={"confirmer": confirmer, "operation_id": operation_id,
                                "status": "unknown" if error == "status_unknown" else "failed" if error else "completed"})


def list_tools():
    ensure_defaults()
    return [{k: v for k, v in vars(s).items() if k not in {"fn", "identity_params"}}
            for s in (get(name) for name in sorted(_REGISTRY)) if s is not None]


def ensure_defaults():
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    for module in (
        "app.db", "app.tools.writing", "app.tools.crawl",
        "app.agent.memory", "app.agent.memory.episodic", "app.agent.memory.summary", "app.agent.memory.card",
        "app.agent.memory.retrieval",
        "app.agent.proactive.jobs", "app.agent.schedules.tools", "app.sports.tools", "app.companion.tools",
    ):
        importlib.import_module(module)
    for name, spec in _DECLARED.items():
        _REGISTRY.setdefault(name, spec)
    _BOOTSTRAPPED = True


def reset_for_tests():
    global _BOOTSTRAPPED
    _REGISTRY.clear()
    _BOOTSTRAPPED = False
