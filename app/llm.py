"""Legacy tier resolution (env → model/base/key), offline helpers and compat shims.

``resolve_tier`` seeds the default model registry of ``app.gateway``; the actual
HTTP calls, fallback chain, limits and usage records live in ``app.gateway``.
Missing keys fall back to the other tier when the vendor matches, otherwise to
offline mock for that call.
"""

from __future__ import annotations

import os
import contextvars
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


class ModelTier(str, Enum):
    """Route by task difficulty so users wait less on easy steps."""

    FAST = "fast"  # cheap / low latency
    STRONG = "strong"  # harder reasoning, longer context


# Provider presets: guessed from model id or base URL
_PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": "KIMI_API_KEY",
        "alt_key_env": "MOONSHOT_API_KEY",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": "KIMI_API_KEY",
        "alt_key_env": "MOONSHOT_API_KEY",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "alt_key_env": "",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "alt_key_env": "",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "alt_key_env": "",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_env": "OPENAI_API_KEY",
        "alt_key_env": "",
    },
}


def guess_provider(model: str, base_url: str = "") -> str:
    mid = (model or "").lower()
    url = (base_url or "").lower()
    if "deepseek" in mid or "deepseek" in url:
        return "deepseek"
    if "qwen" in mid or "dashscope" in mid or "dashscope" in url:
        return "qwen"
    if "kimi" in mid or "moonshot" in mid or "moonshot" in url or "kimi.ai" in url:
        return "kimi"
    if "openai" in url or mid.startswith("gpt-"):
        return "openai"
    if "moonshot" in url or "kimi" in url:
        return "kimi"
    return "openai"


def _env(*names: str) -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return ""


def _max_retries() -> int:
    raw = (_env("LLM_MAX_RETRIES") or "2").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def _default_model(tier: ModelTier) -> str:
    if tier == ModelTier.FAST:
        return _env("MODEL_FAST", "OPENAI_MODEL_FAST") or "deepseek-chat"
    return _env("MODEL_STRONG", "OPENAI_MODEL_STRONG") or "kimi-k3"


def _tier_prefix(tier: ModelTier) -> str:
    return "LLM_FAST" if tier == ModelTier.FAST else "LLM_STRONG"


def resolve_tier(tier: ModelTier) -> dict[str, Any]:
    """Resolve model / base_url / api_key / provider for a tier.

    Key order: tier-specific → provider-guessed → OPENAI_API_KEY → KIMI_API_KEY.
    If this tier has no key, try the other tier when same vendor; else mock.
    """
    prefix = _tier_prefix(tier)
    other = ModelTier.STRONG if tier == ModelTier.FAST else ModelTier.FAST
    other_prefix = _tier_prefix(other)

    model = _env(f"{prefix}_MODEL") or _default_model(tier)

    # Base: LLM_*_BASE_URL → provider preset from model id → OPENAI_BASE_URL → openai
    provider_from_model = guess_provider(model, "")
    preset_from_model = _PROVIDER_PRESETS.get(provider_from_model, {})
    base = (
        _env(f"{prefix}_BASE_URL")
        or preset_from_model.get("base_url")
        or _env("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    base = base.rstrip("/")

    provider = guess_provider(model, base)
    preset = _PROVIDER_PRESETS.get(provider, {})

    tier_key = _env(f"{prefix}_API_KEY")
    provider_key = _env(preset.get("key_env", ""), preset.get("alt_key_env", ""))
    openai_key = _env("OPENAI_API_KEY")
    kimi_key = _env("KIMI_API_KEY", "MOONSHOT_API_KEY")

    # Only use OPENAI_/KIMI_ fallbacks when the tier's vendor matches —
    # never send a Kimi key to DeepSeek/Qwen (would 401 and confuse ops).
    if tier_key:
        key, source = tier_key, "tier"
    elif provider_key:
        key, source = provider_key, "provider"
    elif openai_key and provider in ("openai",):
        key, source = openai_key, "openai"
    elif kimi_key and provider == "kimi":
        key, source = kimi_key, "kimi"
    elif openai_key and provider == "kimi":
        # single-provider setups often put Moonshot key in OPENAI_API_KEY
        key, source = openai_key, "openai"
    else:
        key, source = "", "none"

    # Cross-tier fallback when same vendor
    if not key:
        other_model = _env(f"{other_prefix}_MODEL") or _default_model(other)
        other_base = (
            _env(f"{other_prefix}_BASE_URL")
            or _PROVIDER_PRESETS.get(guess_provider(other_model, ""), {}).get("base_url")
            or base
        )
        other_provider = guess_provider(other_model, other_base)
        other_preset = _PROVIDER_PRESETS.get(other_provider, {})
        other_key = _env(f"{other_prefix}_API_KEY") or _env(
            other_preset.get("key_env", ""), other_preset.get("alt_key_env", "")
        )
        if other_key and other_provider == provider:
            key = other_key
            source = f"other_tier:{other.value}"
            if not _env(f"{prefix}_BASE_URL"):
                base = str(other_base).rstrip("/")

    live = bool(key)
    return {
        "tier": tier.value,
        "model": model,
        "base_url": base,
        "provider": provider,
        "has_key": live,
        "key_source": source,
        "api_key": key,  # never expose in health; stripped in routing_config
        "mode": "live" if live else "mock",
    }


def has_llm(tier: Optional[ModelTier] = None) -> bool:
    if tier is None:
        return resolve_tier(ModelTier.FAST)["has_key"] or resolve_tier(ModelTier.STRONG)["has_key"]
    return bool(resolve_tier(tier)["has_key"])


def model_for(tier: ModelTier) -> str:
    return resolve_tier(tier)["model"]


def base_url(tier: Optional[ModelTier] = None) -> str:
    t = tier or ModelTier.FAST
    return resolve_tier(t)["base_url"]


def routing_config() -> dict[str, Any]:
    """Public health / routing snapshot (no secrets)."""
    fast = resolve_tier(ModelTier.FAST)
    strong = resolve_tier(ModelTier.STRONG)
    from app.gateway.config import get_config

    gateway = get_config()

    def _public(cfg: dict[str, Any], tier: str) -> dict[str, Any]:
        # The gateway chain may borrow the other tier's keyed models (single-key
        # setups); report the chain that will actually serve requests.
        chain = [spec.id for spec in gateway.chain(tier)]
        live = any(spec.has_key() for spec in gateway.chain(tier))
        return {
            "model": cfg["model"],
            "base_url": cfg["base_url"],
            "provider": cfg["provider"],
            "has_key": cfg["has_key"],
            "key_source": cfg["key_source"] if cfg["has_key"] else "none",
            "mode": "live" if live else cfg["mode"],
            "chain": chain,
        }

    return {
        "has_key": has_llm(),
        "fast": _public(fast, "fast"),
        "strong": _public(strong, "strong"),
        "timeouts": {
            "fast": _timeout_for(ModelTier.FAST),
            "strong": _timeout_for(ModelTier.STRONG),
        },
        "empty_escalate_fast_to_strong": (_env("LLM_EMPTY_ESCALATE") or "1")
        not in ("0", "false", "False", "no"),
        "max_retries": _max_retries(),
        # backwards-compatible flat fields
        "base_url": fast["base_url"],
        "fast_model": fast["model"],
        "strong_model": strong["model"],
        "policy": {
            "chat": "fast",
            "draft": "fast|strong(config)",
            "summary": "fast",
        },
        "vendors_documented": {
            "kimi": {"base": "https://api.moonshot.cn/v1", "models": ["kimi-k2.6", "kimi-k3"]},
            "deepseek": {"base": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"]},
            "qwen": {
                "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "models": ["qwen-turbo", "qwen-plus"],
                "key": "DASHSCOPE_API_KEY",
            },
            "openai": {"base": "https://api.openai.com/v1", "models": ["gpt-4o-mini", "gpt-4o"]},
        },
    }


def capabilities() -> dict[str, Any]:
    """Skill/tool list for the Personal Agent shape."""
    return {
        "tools": [
            {
                "id": "chat",
                "name": "对话与记忆",
                "tier": "fast",
                "description": "带长期记忆的对话；记住姓名、城市、偏好和备注",
            },
            {
                "id": "draft_text",
                "name": "写作草稿",
                "tier": "fast|strong",
                "description": "邮件、消息、文章等草稿；确认后才算数，不会自动发出",
            },
            {
                "id": "schedules",
                "name": "提醒与定时任务",
                "tier": "fast",
                "description": "一次性提醒和自定义只读定时任务，确认卡后创建",
            },
            {
                "id": "web_read",
                "name": "联网读取",
                "tier": "fast",
                "description": "读取公开网页、RSS、搜索结果作为不可信来源",
            },
        ],
        "hitl": ["draft_text", "schedule.create", "schedule.update", "schedule.delete"],
        "routing": routing_config(),
    }


def _timeout_for(tier: ModelTier, override: Optional[float] = None) -> float:
    """Env-overridable timeouts: LLM_FAST_TIMEOUT / LLM_STRONG_TIMEOUT."""
    if override is not None:
        return float(override)
    if tier == ModelTier.FAST:
        return float(_env("LLM_FAST_TIMEOUT") or "25")
    return float(_env("LLM_STRONG_TIMEOUT") or "60")


_call_meta: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "llm_call_meta", default=None
)


def begin_call_meta() -> dict[str, Any]:
    meta: dict[str, Any] = {
        "requested_model": "",
        "actual_model": "",
        "provider": "",
        "mode": "",
        "attempts": 0,
        "attempt_errors": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "reported": False},
        "error": "",
        "fallback_reason": "",
        "model_fallback": "",
    }
    _call_meta.set(meta)
    return meta


def end_call_meta() -> None:
    _call_meta.set(None)


def _meta() -> dict[str, Any]:
    m = _call_meta.get()
    if m is None:
        return {"attempt_errors": [], "usage": {}}
    return m


def _short_error(exc: BaseException) -> str:
    from app.agent.harness.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}", max_len=200)


def _note_attempt(model: str, resp: Any = None, exc: Optional[BaseException] = None) -> None:
    m = _meta()
    m["attempts"] = int(m.get("attempts") or 0) + 1
    if exc is not None:
        m["attempt_errors"].append({"model": model, "error": type(exc).__name__})
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    u = m.setdefault("usage", {})
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        try:
            u[k] = int(u.get(k) or 0) + int(getattr(usage, k, 0) or 0)
        except (TypeError, ValueError):
            continue
    u["reported"] = True


def _note_result(*, actual_model: str = "", fallback_reason: str = "", error: str = "", mode: str = "") -> None:
    m = _meta()
    if actual_model:
        m["actual_model"] = actual_model
    if fallback_reason and not m.get("fallback_reason"):
        m["fallback_reason"] = fallback_reason
    if error:
        m["error"] = error
    if mode:
        m["mode"] = mode


def chat_json(
    system: str,
    user: str,
    *,
    tier: ModelTier = ModelTier.FAST,
    fallback: Optional[dict[str, Any]] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    """Compat shim; implementation lives in ``app.gateway.llm_gateway``."""
    from app.gateway.llm_gateway import chat_json as _gw

    return _gw(system, user, tier=tier, fallback=fallback, timeout=timeout)


def chat_text(
    system: str,
    user: str,
    *,
    tier: ModelTier = ModelTier.FAST,
    fallback: str = "",
    timeout: Optional[float] = None,
) -> str:
    """Compat shim; implementation lives in ``app.gateway.llm_gateway``."""
    from app.gateway.llm_gateway import chat_text as _gw

    return _gw(system, user, tier=tier, fallback=fallback, timeout=timeout)


def _mock_text(user: str) -> str:
    return (
        f"[mock-llm] 已收到，长度 {len(user)} 字。"
        "请配置 LLM_FAST_API_KEY / LLM_STRONG_API_KEY（或 DEEPSEEK_API_KEY / KIMI_API_KEY / DASHSCOPE_API_KEY）使用真实模型。"
    )
