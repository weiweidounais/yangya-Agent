"""Task difficulty tiers — one classifier for model routing and loop budgets.

``classify_difficulty`` decides simple / medium / hard. The gateway routes hard
tasks to the STRONG model (everything else FAST), and the agent loop sizes the
task budget from the same tier, so "needs the strong model" and "gets the big
budget" can never disagree.

Budgets per tier (0 disables a dimension), env-overridable:
  AGENT_BUDGET_<TIER>_STEPS / _SECONDS / _TOKENS / _COST   (TIER = SIMPLE|MEDIUM|HARD)
A "continue" click at the limit adds 50% of the current tier's base limits.
Legacy single-budget names (AGENT_MAX_STEPS / _SECONDS / _TOKENS, AGENT_MAX_COST)
are still honoured as a fallback for the MEDIUM tier (the old budget's scope),
and AGENT_MAX_COST also for any tier without its own _COST.
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from app.llm import ModelTier

Tier = Literal["simple", "medium", "hard"]
SIMPLE: Tier = "simple"
MEDIUM: Tier = "medium"
HARD: Tier = "hard"
TIER_ORDER: tuple[Tier, ...] = (SIMPLE, MEDIUM, HARD)
TIER_ZH = {SIMPLE: "简单", MEDIUM: "中等", HARD: "困难"}

# steps, seconds, tokens
TIER_DEFAULTS: dict[str, tuple[int, float, int]] = {
    SIMPLE: (15, 180.0, 100_000),
    MEDIUM: (50, 900.0, 400_000),
    HARD: (120, 2700.0, 1_500_000),
}
CONTINUE_FRACTION = 0.5

TASK_DIFFICULTY: dict[str, Tier] = {
    # simple: chat / bookkeeping
    "chat": SIMPLE,
    "update_profile": SIMPLE,
    "confirm": SIMPLE,
    "summary": SIMPLE,
    "memory_extract": SIMPLE,
    "memory_reconcile": SIMPLE,
    "risk_classify": SIMPLE,
    "tool_selection": SIMPLE,
    # medium: writing drafts
    "draft": MEDIUM,
    "draft_text": MEDIUM,
    # hard: long-form reasoning
    "strong": HARD,
    "reason": HARD,
}


def classify_difficulty(task: str, **_: object) -> Tier:
    """Difficulty for a logical task."""
    t = (task or "").strip().lower()
    return TASK_DIFFICULTY.get(t, SIMPLE)


def model_tier_for(tier: Tier) -> ModelTier:
    return ModelTier.STRONG if tier == HARD else ModelTier.FAST


def normalize_tier(tier: Optional[str]) -> Tier:
    t = str(tier or "").strip().lower()
    return t if t in TIER_ORDER else MEDIUM  # type: ignore[return-value]


def next_tier(tier: Optional[str]) -> Optional[Tier]:
    t = normalize_tier(tier)
    i = TIER_ORDER.index(t)
    return TIER_ORDER[i + 1] if i + 1 < len(TIER_ORDER) else None


def tier_rank(tier: Optional[str]) -> int:
    return TIER_ORDER.index(normalize_tier(tier))


def _env_num(names: tuple[str, ...], cast, default):
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if raw:
            try:
                return cast(raw)
            except ValueError:
                continue
    return default


def tier_limits(tier: Optional[str]) -> dict[str, float]:
    """Resolve steps / seconds / tokens / cost for a tier from env + defaults."""
    t = normalize_tier(tier)
    up = t.upper()
    steps, seconds, tokens = TIER_DEFAULTS[t]
    legacy = t == MEDIUM
    return {
        "max_steps": _env_num(
            (f"AGENT_BUDGET_{up}_STEPS",) + (("AGENT_MAX_STEPS",) if legacy else ()), int, steps
        ),
        "max_seconds": _env_num(
            (f"AGENT_BUDGET_{up}_SECONDS",) + (("AGENT_MAX_SECONDS",) if legacy else ()), float, seconds
        ),
        "max_tokens": _env_num(
            (f"AGENT_BUDGET_{up}_TOKENS",) + (("AGENT_MAX_TOKENS",) if legacy else ()), int, tokens
        ),
        "max_cost": _env_num(
            (f"AGENT_BUDGET_{up}_COST", "AGENT_MAX_COST", "AGENT_COST_SOFT_LIMIT"), float, 0.0
        ),
    }


def continue_extension(tier: Optional[str]) -> dict[str, float]:
    """What one "continue" click adds: 50% of the tier's base limits."""
    lim = tier_limits(tier)
    return {
        "max_steps": max(1, int(lim["max_steps"] * CONTINUE_FRACTION)),
        "max_seconds": float(lim["max_seconds"]) * CONTINUE_FRACTION,
        "max_tokens": int(lim["max_tokens"] * CONTINUE_FRACTION),
        "max_cost": float(lim["max_cost"]) * CONTINUE_FRACTION,
    }
