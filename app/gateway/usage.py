"""Per-call usage records: model, latency, tokens, estimated cost.

Every upstream attempt is logged as one JSON line on the ``llm.usage`` logger,
kept in an in-memory ring buffer (``/api/llm/usage``), and optionally appended
to ``LLM_USAGE_LOG`` (JSONL path).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Optional

from app.gateway.config import ModelSpec

log = logging.getLogger("llm.usage")

_RING: deque[dict[str, Any]] = deque(maxlen=int(os.getenv("LLM_USAGE_RING", "500") or 500))
_lock = threading.Lock()
_totals: dict[str, dict[str, Any]] = {}


def estimate_cost(spec: ModelSpec, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
    if spec.price_input is None and spec.price_output is None:
        return None
    if prompt_tokens is None and completion_tokens is None:
        return None
    cost = (prompt_tokens or 0) * (spec.price_input or 0.0) + (completion_tokens or 0) * (spec.price_output or 0.0)
    return round(cost / 1_000_000, 6)


def record(
    *,
    request_id: str,
    task: str,
    tier: str,
    spec: ModelSpec,
    attempt: int,
    ok: bool,
    latency_ms: float,
    error: str = "",
    status: Optional[int] = None,
    prompt_tokens: Optional[int] = None,
    completion_tokens: Optional[int] = None,
    queue_ms: float = 0.0,
) -> dict[str, Any]:
    cost = estimate_cost(spec, prompt_tokens, completion_tokens)
    rec = {
        "ts": round(time.time(), 3),
        "request_id": request_id,
        "task": task,
        "tier": tier,
        "model_id": spec.id,
        "model": spec.model,
        "protocol": spec.protocol,
        "vendor": spec.vendor,
        "attempt": attempt,
        "ok": ok,
        "error": error,
        "status": status,
        "latency_ms": round(latency_ms, 1),
        "queue_ms": round(queue_ms, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost": cost,
        "currency": spec.currency if cost is not None else None,
    }
    with _lock:
        _RING.append(rec)
        t = _totals.setdefault(
            spec.id,
            {
                "calls": 0,
                "ok": 0,
                "errors": {},
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
                "currency": spec.currency,
                "latency_ms_sum": 0.0,
            },
        )
        t["calls"] += 1
        t["latency_ms_sum"] += latency_ms
        if ok:
            t["ok"] += 1
        else:
            t["errors"][error or "error"] = t["errors"].get(error or "error", 0) + 1
        t["prompt_tokens"] += prompt_tokens or 0
        t["completion_tokens"] += completion_tokens or 0
        t["cost"] = round(t["cost"] + (cost or 0.0), 6)
    line = json.dumps(rec, ensure_ascii=False)
    if ok:
        log.info(line)
    else:
        log.warning(line)
    path = (os.getenv("LLM_USAGE_LOG") or "").strip()
    if path:
        try:
            with _lock, open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    return rec


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        items = list(_RING)
    return items[-max(0, limit):]


def totals() -> dict[str, dict[str, Any]]:
    with _lock:
        out = {}
        for mid, t in _totals.items():
            d = dict(t)
            d["errors"] = dict(t["errors"])
            d["avg_latency_ms"] = round(t["latency_ms_sum"] / t["calls"], 1) if t["calls"] else 0.0
            d.pop("latency_ms_sum", None)
            out[mid] = d
        return out


def reset() -> None:
    with _lock:
        _RING.clear()
        _totals.clear()


def public_snapshot(limit: int = 50) -> dict[str, Any]:
    """Allowlisted metrics only; task/error strings may contain caller content."""
    from app.gateway import limiter, providers

    numeric = ("calls", "ok", "prompt_tokens", "completion_tokens", "cost", "avg_latency_ms")
    public_totals = {
        model: {**{key: values[key] for key in numeric},
                "errors": sum(values["errors"].values()), "currency": values["currency"]}
        for model, values in totals().items()
    }
    # Fixed task labels are reference data, never arbitrary caller-supplied text.
    tasks = {"chat", "draft", "summary", "tool_selection", "risk_classify", "memory_extract", "memory_reconcile"}
    attempts = []
    for row in recent(limit) if limit > 0 else []:
        metrics = {key: row[key] for key in
                   ("ok", "latency_ms", "queue_ms", "prompt_tokens", "completion_tokens", "cost")}
        metrics["task"] = row["task"] if row["task"] in tasks else "other"
        attempts.append(metrics)
    return {"totals": public_totals, "recent": attempts,
            "limits": limiter.stats(), "http_pool": providers.pool_stats()}
