"""Run a rubric: LLM judge when a real model is configured, heuristic otherwise.

``JUDGE_MODE``: ``auto`` (default: LLM if a vendor key is set, else heuristic),
``llm`` (LLM; heuristic only if the call fails), ``heuristic`` (never call a model).
The golden mock LLM override is never used as a judge.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from app.evals.rubrics import RUBRICS, Rubric


def _mode(mode: Optional[str]) -> str:
    m = (mode or os.getenv("JUDGE_MODE") or "auto").strip().lower()
    return m if m in ("auto", "llm", "heuristic") else "auto"


def _real_llm_available() -> bool:
    from app.gateway.llm_gateway import get_llm_override
    from app.llm import ModelTier, has_llm

    return get_llm_override() is None and has_llm(ModelTier.STRONG)


def _parse(raw: Any) -> Optional[tuple[float, str]]:
    if isinstance(raw, dict) and "score" in raw:
        data = raw
    else:
        m = re.search(r"\{[\s\S]*\}", str(raw or ""))
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return None
    try:
        score = float(data.get("score"))
    except (TypeError, ValueError):
        return None
    if score > 1.0 and score <= 10.0:
        score /= 10.0
    return max(0.0, min(1.0, score)), str(data.get("reason") or "")[:200]


def _llm_judge(rubric: Rubric, *, input: str, output: str, context: str) -> Optional[tuple[float, str, str]]:
    from app.gateway.llm_gateway import chat_text
    from app.llm import ModelTier

    prompt = rubric.render(input=input, output=output, context=context)
    raw = chat_text(
        "你是严格、客观的评审，只按给定标准打分。",
        prompt,
        tier=ModelTier.STRONG,
        fallback="",
        timeout=float(os.getenv("JUDGE_TIMEOUT_S") or 60),
    )
    parsed = _parse(raw)
    if parsed is None:
        return None
    from app.llm import model_for

    return parsed[0], parsed[1], model_for(ModelTier.STRONG)


def judge(
    rubric: str | Rubric,
    *,
    input: str,
    output: str,
    context: str = "",
    mode: Optional[str] = None,
) -> dict[str, Any]:
    """{"rubric", "version", "score", "pass", "threshold", "reason", "method", "model"}."""
    r = RUBRICS[rubric] if isinstance(rubric, str) else rubric
    m = _mode(mode)
    result: Optional[tuple[float, str, str]] = None
    method = "heuristic"
    error = ""
    if m == "llm" or (m == "auto" and _real_llm_available()):
        try:
            result = _llm_judge(r, input=input, output=output, context=context)
            method = "llm" if result else "heuristic"
            if result is None:
                error = "unparseable judge output"
        except Exception as exc:
            error = type(exc).__name__
    if result is None:
        s, why = r.heuristic(input or "", output or "", context or "")
        result = (s, why, "")
    score, reason, model = result
    return {
        "rubric": r.name,
        "version": r.version,
        "score": round(float(score), 3),
        "threshold": r.threshold,
        "pass": float(score) >= r.threshold,
        "safety": r.safety,
        "reason": reason,
        "method": method,
        "model": model,
        **({"judge_error": error} if error else {}),
    }
