"""``@traced_tool`` — a Langfuse ``tool`` observation around a tool function."""

from __future__ import annotations

import functools
from contextlib import nullcontext
import time
from typing import Any, Callable, TypeVar

from app.observability import tracing

F = TypeVar("F", bound=Callable[..., Any])


def _shape(v: Any) -> Any:
    """Argument / result summary: sizes and keys, never full documents."""
    if isinstance(v, str):
        return f"str[{len(v)}]"
    if isinstance(v, bytes):
        return f"bytes[{len(v)}]"
    if isinstance(v, dict):
        return {"keys": sorted(str(k) for k in v)[:20]}
    if isinstance(v, (list, tuple)):
        return f"list[{len(v)}]"
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return type(v).__name__


def traced_tool(name: str) -> Callable[[F], F]:
    def deco(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from app.agent.tool_registry import _TRACE_ENABLED
            observation = (tracing.observe(f"tool.{name}", kind="tool")
                           if _TRACE_ENABLED.get() else nullcontext(tracing.Observation()))
            with observation as obs:
                t0 = time.perf_counter()
                if obs.recording:
                    obs.input({"args": [_shape(a) for a in args], "kwargs": {k: _shape(v) for k, v in kwargs.items()}})
                result = fn(*args, **kwargs)
                if obs.recording:
                    obs.output(_shape(result)).meta(tool=name, latency_ms=round((time.perf_counter() - t0) * 1000.0, 1))
                return result

        return wrapper  # type: ignore[return-value]

    return deco
