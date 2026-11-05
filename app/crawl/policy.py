"""Only operator configuration can change policy; request arguments cannot."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import NotConfigured

CONFIG = Path(__file__).resolve().parents[2] / "config" / "crawl_policy.yaml"


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    tos_risk: str = "low"
    proxy: bool = False
    robots_respect: bool = True
    rate_requests: int = 1
    rate_seconds: float = 2
    daily_cap: int = 1000
    cache_ttl_s: int = 7200

    def public(self):
        return asdict(self)


def load_config(path=None):
    import yaml

    try:
        config = yaml.safe_load(Path(path or CONFIG).read_text())
        override = json.loads(os.getenv("CRAWL_POLICY_OVERRIDES", "{}"))
        for name, patch in override.items():
            config["adapters"].setdefault(name, {}).update(patch)
        return config
    except (OSError, ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError):
        raise NotConfigured("Invalid crawl policy configuration") from None


def get_policy(name, config):
    row = {**config.get("defaults", {}), **config.get("adapters", {}).get(name, {})}
    if name not in config.get("adapters", {}):
        return Policy()
    try:
        policy = Policy(**row)
        if (any(type(getattr(policy, k)) is not bool for k in ("enabled", "proxy", "robots_respect"))
                or policy.tos_risk not in {"low", "medium", "high"}
                or type(policy.rate_requests) is not int or policy.rate_requests < 1
                or not 0 < policy.rate_seconds <= 86400
                or type(policy.daily_cap) is not int or policy.daily_cap < 0
                or type(policy.cache_ttl_s) is not int or policy.cache_ttl_s < 0):
            raise ValueError()
        return policy
    except (TypeError, ValueError):
        raise NotConfigured("Invalid adapter policy") from None
