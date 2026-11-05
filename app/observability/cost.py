"""Per-call LLM cost from a configurable price table.

``LLM_PRICES_JSON`` maps a model name (exact, or a prefix ending in ``*``) to
prices per 1M tokens, e.g.::

    {"deepseek-chat": {"input": 0.27, "output": 1.10},
     "moonshot-v1-*": {"input": 1.65, "output": 1.65},
     "*": {"input": 0, "output": 0}}

``LLM_PRICE_CURRENCY`` (default USD) is reported alongside. Unknown model → no cost.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Optional


@lru_cache(maxsize=4)
def _table(raw: str) -> dict[str, dict[str, float]]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict[str, float]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                try:
                    out[str(k)] = {"input": float(v.get("input") or 0), "output": float(v.get("output") or 0)}
                except (TypeError, ValueError):
                    continue
    return out


def price_for(model: str) -> Optional[dict[str, float]]:
    table = _table(os.getenv("LLM_PRICES_JSON") or "")
    if not table or not model:
        return table.get("*") if table else None
    if model in table:
        return table[model]
    best = ""
    for k in table:
        if k.endswith("*") and k != "*" and model.startswith(k[:-1]) and len(k) > len(best):
            best = k
    return table.get(best) if best else table.get("*")


def cost_for(model: str, input_tokens: int, output_tokens: int) -> Optional[dict[str, Any]]:
    p = price_for(model)
    if p is None:
        return None
    ci = p["input"] * max(0, int(input_tokens or 0)) / 1_000_000
    co = p["output"] * max(0, int(output_tokens or 0)) / 1_000_000
    return {
        "input": round(ci, 8),
        "output": round(co, 8),
        "total": round(ci + co, 8),
        "currency": (os.getenv("LLM_PRICE_CURRENCY") or "USD").upper(),
    }
