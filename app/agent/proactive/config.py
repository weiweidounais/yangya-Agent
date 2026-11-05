"""Proactive execution env config. Default OFF for safety."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ProactiveConfig:
    """Soft budgets + wake loop for user schedules (reminders, custom tasks)."""

    enabled: bool = False
    tz: str = "Asia/Shanghai"
    # Wake loop period (seconds)
    tick_seconds: float = 60.0
    # Soft budgets
    max_users_per_tick: int = 50
    soft_budget_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "ProactiveConfig":
        return cls(
            enabled=_truthy("PROACTIVE_ENABLED", False),
            tz=(os.getenv("PROACTIVE_TZ") or "Asia/Shanghai").strip() or "Asia/Shanghai",
            tick_seconds=max(1.0, _float_env("PROACTIVE_TICK_SECONDS", 60.0)),
            max_users_per_tick=max(1, _int_env("PROACTIVE_MAX_USERS_PER_TICK", 50)),
            soft_budget_seconds=max(1.0, _float_env("PROACTIVE_SOFT_BUDGET_SECONDS", 30.0)),
        )

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "tz": self.tz,
            "tick_seconds": self.tick_seconds,
            "max_users_per_tick": self.max_users_per_tick,
            "soft_budget_seconds": self.soft_budget_seconds,
        }


def get_config() -> ProactiveConfig:
    return ProactiveConfig.from_env()
