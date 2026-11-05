"""Model registry + task/tier routing config.

Sources (later wins):

1. Legacy env (``LLM_FAST_*`` / ``LLM_STRONG_*`` / ``MODEL_*`` / provider keys),
   resolved through ``app.llm.resolve_tier`` — this is the default and keeps the
   current two-tier Kimi deployment byte-for-byte equivalent in model choice.
2. ``LLM_CONFIG_FILE`` (JSON, or TOML when the path ends in ``.toml``).
3. ``LLM_CONFIG_JSON`` (inline JSON, handy for PaaS env-only setups).
4. ``LLM_FAST_CHAIN`` / ``LLM_STRONG_CHAIN`` (comma-separated model ids).

See ``docs/llm-gateway.md`` for the schema.
"""

from __future__ import annotations

import logging

import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

PROTOCOL_OPENAI = "openai"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOLS = (PROTOCOL_OPENAI, PROTOCOL_ANTHROPIC)

TIERS = ("fast", "strong")

DEFAULT_TASK_TIERS: dict[str, str] = {
    "strong": "strong",
    "reason": "strong",
    "draft": "fast",
    "chat": "fast",
    "summary": "fast",
    "tool_selection": "fast",
    "memory_extract": "fast",
    "memory_reconcile": "fast",
    "risk_classify": "fast",
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    model: str
    protocol: str = PROTOCOL_OPENAI
    base_url: str = ""
    api_key_env: tuple[str, ...] = ()
    vendor: str = ""
    # Price per 1M tokens; None → cost not estimated.
    price_input: Optional[float] = None
    price_output: Optional[float] = None
    currency: str = "CNY"
    timeout: Optional[float] = None
    max_tokens: Optional[int] = None
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    # Upstream account bucket shared by models on the same key (Kimi k2.6 + k3).
    limit_group: str = ""
    max_concurrency: Optional[int] = None
    rpm: Optional[int] = None
    # Legacy specs resolve their key through app.llm.resolve_tier(tier).
    legacy_tier: str = ""

    def api_key(self) -> str:
        if self.legacy_tier:
            from app.llm import ModelTier, resolve_tier

            return str(resolve_tier(ModelTier(self.legacy_tier)).get("api_key") or "")
        for name in self.api_key_env:
            v = (os.getenv(name) or "").strip()
            if v:
                return v
        return ""

    def has_key(self) -> bool:
        return bool(self.api_key())

    def group(self) -> str:
        if self.limit_group:
            return self.limit_group
        return f"{self.base_url}|{','.join(self.api_key_env) or self.legacy_tier}"

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "protocol": self.protocol,
            "vendor": self.vendor,
            "base_url": self.base_url,
            "api_key_env": list(self.api_key_env) or (["<legacy:" + self.legacy_tier + ">"] if self.legacy_tier else []),
            "has_key": self.has_key(),
            "price_per_1m": {
                "input": self.price_input,
                "output": self.price_output,
                "currency": self.currency,
            },
            "limit_group": self.group(),
            "max_concurrency": self.max_concurrency,
            "rpm": self.rpm,
        }


@dataclass
class GatewayConfig:
    models: dict[str, ModelSpec]
    tiers: dict[str, list[str]]
    tasks: dict[str, str]
    sources: list[str]
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_chains: dict[str, list[str]] = field(default_factory=dict)
    embedding_tasks: dict[str, str] = field(default_factory=dict)
    rerank_tasks: dict[str, str] = field(default_factory=dict)

    def rerank_model(self, task: str = "memory_rerank") -> ModelSpec:
        if task in self.rerank_tasks:
            return self.models[self.rerank_tasks[task]]
        if task != "memory_rerank":
            raise ConfigError(f"unknown rerank task {task!r}")
        return ModelSpec(
            id="siliconflow-bge-reranker-v2-m3", model="BAAI/bge-reranker-v2-m3",
            vendor="siliconflow", base_url="https://api.siliconflow.cn/v1",
            api_key_env=("SILICONFLOW_API_KEY",), timeout=2.0,
        )

    def embedding_model(self, task: str = "memory_embed") -> ModelSpec:
        if task in self.embedding_tasks:
            return self.models[self.embedding_tasks[task]]
        if task != "memory_embed":
            raise ConfigError(f"unknown embedding task {task!r}")
        return ModelSpec(
            id="siliconflow-bge-m3", model="BAAI/bge-m3", vendor="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            api_key_env=("SILICONFLOW_API_KEY",), timeout=20.0,
        )

    def chain(self, tier: str) -> list[ModelSpec]:
        return [self.models[i] for i in self.tiers.get(tier, []) if i in self.models]

    def task_chain(self, task: str, tier: str) -> list[ModelSpec]:
        task = task.strip().lower()
        ids = self.task_chains.get(task)
        target = self.tasks.get(task)
        if ids is None and target in self.models:
            ids = [target, "fast", "strong"]
        if ids is None:
            return self.chain(tier)
        chain: dict[str, ModelSpec] = {}
        for name in ids:
            for spec in self.chain(name) if name in TIERS else [self.models[name]]:
                chain.setdefault(spec.id, spec)
        return list(chain.values())

    def public(self) -> dict[str, Any]:
        return {
            "sources": list(self.sources),
            "tiers": {k: list(v) for k, v in self.tiers.items()},
            "tasks": dict(self.tasks),
            "task_chains": {k: list(v) for k, v in self.task_chains.items()},
            "models": {k: m.public() for k, m in self.models.items()},
        }


class ConfigError(ValueError):
    pass


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    return float(v)


def _int_or_none(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    return int(v)


def _legacy_params(prefix: str) -> dict[str, Any]:
    """``LLM_FAST_PARAMS`` / ``LLM_STRONG_PARAMS``: a JSON object merged into the request body.

    Typical use: ``{"enable_thinking": false}`` so a reasoning model does not spend a
    small ``max_tokens`` budget (tool selection, classifiers) on hidden thinking and
    return empty content. Invalid values are ignored with a warning, never fatal.
    """
    raw = _env(f"{prefix}_PARAMS")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        logging.getLogger(__name__).warning("%s_PARAMS ignored: expected a JSON object", prefix)
        return {}
    return parsed


def _legacy_models() -> tuple[dict[str, ModelSpec], dict[str, list[str]]]:
    from app.llm import ModelTier, resolve_tier

    models: dict[str, ModelSpec] = {}
    tiers: dict[str, list[str]] = {"fast": [], "strong": []}

    def _add(spec: ModelSpec, tier: str) -> None:
        mid = spec.id
        existing = models.get(mid)
        if existing and (existing.base_url, existing.legacy_tier) != (spec.base_url, spec.legacy_tier):
            same_upstream = existing.base_url == spec.base_url and existing.api_key() == spec.api_key()
            if not same_upstream:
                mid = f"{spec.id}@{tier}"
                spec = replace(spec, id=mid)
        models.setdefault(mid, spec)
        if mid not in tiers[tier]:
            tiers[tier].append(mid)

    for t in (ModelTier.FAST, ModelTier.STRONG):
        cfg = resolve_tier(t)
        prefix = "LLM_FAST" if t == ModelTier.FAST else "LLM_STRONG"
        spec = ModelSpec(
            id=cfg["model"],
            model=cfg["model"],
            protocol=PROTOCOL_OPENAI,
            base_url=cfg["base_url"],
            vendor=cfg["provider"],
            legacy_tier=t.value,
            price_input=_float_or_none(_env(f"{prefix}_PRICE_INPUT")),
            price_output=_float_or_none(_env(f"{prefix}_PRICE_OUTPUT")),
            params=_legacy_params(prefix),
            limit_group=cfg["base_url"],
        )
        _add(spec, t.value)
        if t == ModelTier.STRONG:
            explicit = _env("LLM_STRONG_FALLBACK_MODEL")
            fb = explicit or ("kimi-k2.6" if "kimi-k3" in cfg["model"].lower() else "")
            if fb and fb != cfg["model"]:
                fb_spec = replace(
                    spec,
                    id=fb,
                    model=fb,
                    price_input=_float_or_none(_env("LLM_STRONG_FALLBACK_PRICE_INPUT")),
                    price_output=_float_or_none(_env("LLM_STRONG_FALLBACK_PRICE_OUTPUT")),
                )
                _add(fb_spec, "strong")
    # Single-key setups: a tier whose own model has no key must not silently run
    # in mock mode while the other tier is live. Fall back to the other tier's
    # keyed models (its own, keyless entry stays first so an operator can still
    # see which model was requested).
    for tier, other in (("fast", "strong"), ("strong", "fast")):
        if any(models[m].has_key() for m in tiers[tier]):
            continue
        borrowed = [m for m in tiers[other] if models[m].has_key() and m not in tiers[tier]]
        if tier == "fast":
            borrowed.reverse()  # strong's fallback (cheaper, faster) first for chat-sized tasks
        if borrowed:
            logging.getLogger(__name__).warning(
                "llm tier %s has no API key for %s; falling back to %s", tier, tiers[tier], borrowed
            )
            tiers[tier].extend(borrowed)
    return models, tiers


def _parse_model(mid: str, raw: dict[str, Any], providers: dict[str, dict[str, Any]]) -> ModelSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"model {mid!r} must be an object")
    prov_name = raw.get("provider") or ""
    prov = dict(providers.get(prov_name) or {}) if prov_name else {}
    if prov_name and prov_name not in providers:
        raise ConfigError(f"model {mid!r} references unknown provider {prov_name!r}")
    merged = {**prov, **raw}
    protocol = str(merged.get("protocol") or PROTOCOL_OPENAI).lower()
    if protocol not in PROTOCOLS:
        raise ConfigError(f"model {mid!r}: protocol must be one of {PROTOCOLS}")
    base = str(merged.get("base_url") or "").rstrip("/")
    if not base:
        base = "https://api.anthropic.com" if protocol == PROTOCOL_ANTHROPIC else ""
    if not base:
        raise ConfigError(f"model {mid!r}: base_url required")
    key_env = merged.get("api_key_env") or ()
    if isinstance(key_env, str):
        key_env = tuple(s.strip() for s in key_env.split(",") if s.strip())
    price = merged.get("price") or {}
    return ModelSpec(
        id=mid,
        model=str(merged.get("model") or mid),
        protocol=protocol,
        base_url=base,
        api_key_env=tuple(key_env),
        vendor=str(merged.get("vendor") or prov_name or protocol),
        price_input=_float_or_none(price.get("input", merged.get("price_input"))),
        price_output=_float_or_none(price.get("output", merged.get("price_output"))),
        currency=str(price.get("currency") or merged.get("currency") or "CNY"),
        timeout=_float_or_none(merged.get("timeout")),
        max_tokens=_int_or_none(merged.get("max_tokens")),
        params=dict(merged.get("params") or {}),
        limit_group=str(merged.get("limit_group") or prov_name or ""),
        max_concurrency=_int_or_none(merged.get("max_concurrency")),
        rpm=_int_or_none(merged.get("rpm")),
    )


def _read_file(path: str) -> dict[str, Any]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".toml":
        import tomllib

        return tomllib.loads(text)
    return json.loads(text)


def _merge_doc(cfg: GatewayConfig, doc: dict[str, Any], source: str) -> None:
    providers = doc.get("providers") or {}
    for name, p in providers.items():
        if p.get("max_concurrency") is not None or p.get("rpm") is not None:
            cfg.groups[name] = {
                "max_concurrency": _int_or_none(p.get("max_concurrency")),
                "rpm": _int_or_none(p.get("rpm")),
            }
    for mid, raw in (doc.get("models") or {}).items():
        cfg.models[mid] = _parse_model(mid, raw, providers)
    for tier, ids in (doc.get("tiers") or {}).items():
        if tier not in TIERS:
            raise ConfigError(f"unknown tier {tier!r}; expected {TIERS}")
        if isinstance(ids, str):
            ids = [s.strip() for s in ids.split(",") if s.strip()]
        cfg.tiers[tier] = list(ids)
    for task, tier in (doc.get("tasks") or {}).items():
        cfg.tasks[str(task).lower()] = str(tier)
    for task, mid in (doc.get("embedding_tasks") or {}).items():
        cfg.embedding_tasks[str(task).lower()] = str(mid)
    for task, mid in (doc.get("rerank_tasks") or {}).items():
        cfg.rerank_tasks[str(task).lower()] = str(mid)
    for task, ids in (doc.get("task_chains") or {}).items():
        cfg.task_chains[str(task).lower()] = list(ids)
    cfg.sources.append(source)


def _validate(cfg: GatewayConfig) -> None:
    for task, mid in cfg.rerank_tasks.items():
        if mid not in cfg.models:
            raise ConfigError(f"rerank task {task!r} references undeclared model {mid!r}")
        if cfg.models[mid].protocol != PROTOCOL_OPENAI:
            raise ConfigError(f"rerank task {task!r} requires the openai protocol")
    for task, mid in cfg.embedding_tasks.items():
        if mid not in cfg.models:
            raise ConfigError(f"embedding task {task!r} references undeclared model {mid!r}")
        if cfg.models[mid].protocol != PROTOCOL_OPENAI:
            raise ConfigError(f"embedding task {task!r} requires the openai protocol")
    for tier, ids in cfg.tiers.items():
        missing = [i for i in ids if i not in cfg.models]
        if missing:
            raise ConfigError(f"tier {tier!r} references undeclared models {missing}")
    for task, tier in cfg.tasks.items():
        if tier not in TIERS and tier not in cfg.models:
            raise ConfigError(f"task {task!r} maps to unknown tier {tier!r}")
    for task, ids in cfg.task_chains.items():
        if not ids or any(i not in TIERS and i not in cfg.models for i in ids):
            raise ConfigError(f"task {task!r} has an empty or undeclared model chain")


def build_config() -> GatewayConfig:
    models, tiers = _legacy_models()
    cfg = GatewayConfig(models=models, tiers=tiers, tasks=dict(DEFAULT_TASK_TIERS), sources=["legacy_env"])
    path = _env("LLM_CONFIG_FILE")
    if path:
        _merge_doc(cfg, _read_file(path), f"file:{path}")
    inline = _env("LLM_CONFIG_JSON")
    if inline:
        _merge_doc(cfg, json.loads(inline), "env:LLM_CONFIG_JSON")
    for tier in TIERS:
        chain = _env(f"LLM_{tier.upper()}_CHAIN")
        if chain:
            cfg.tiers[tier] = [s.strip() for s in chain.split(",") if s.strip()]
            cfg.sources.append(f"env:LLM_{tier.upper()}_CHAIN")
    for task in cfg.tasks.keys() | cfg.task_chains.keys():
        name = f"LLM_{task.upper()}_CHAIN"
        if _env(name):
            cfg.task_chains[task] = [s.strip() for s in _env(name).split(",") if s.strip()]
            cfg.sources.append(f"env:{name}")
    _validate(cfg)
    return cfg


_ENV_PREFIXES = (
    "LLM_", "MODEL_", "OPENAI_", "KIMI_", "MOONSHOT_", "DEEPSEEK_", "DASHSCOPE_", "ANTHROPIC_",
)
_cache_lock = threading.Lock()
_cache: tuple[Any, GatewayConfig] | None = None


def _fingerprint() -> tuple:
    env = tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)))
    mtime = 0.0
    path = _env("LLM_CONFIG_FILE")
    if path:
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = -1.0
    return env, mtime


def get_config() -> GatewayConfig:
    """Cached config; rebuilt automatically when relevant env / file changes."""
    global _cache
    fp = _fingerprint()
    with _cache_lock:
        if _cache is not None and _cache[0] == fp:
            return _cache[1]
    cfg = build_config()
    with _cache_lock:
        _cache = (fp, cfg)
    return cfg


def reset_config_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None
