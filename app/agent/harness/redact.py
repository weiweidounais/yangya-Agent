"""Redaction + token estimation shared by traces and output guardrails.

Never log raw secrets, tokens, contact PII, or privacy-sandbox values.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Iterable

SECRET_ENV_NAME = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|JWT|COOKIE|CREDENTIAL|PRIVATE)", re.I
)
# Matched against the *last segment* of a normalised key so telemetry such as
# ``tokens_in_est`` / ``max_tokens`` / ``prompt_tokens`` is never mistaken for a secret.
SENSITIVE_FIELD = re.compile(
    r"(?:^|_)(?:api_?key|apikey|access_?key|secret_?key|private_?key|token|secret|password|passwd|"
    r"authorization|cookie|jwt|credentials?|session_?id)$",
    re.I,
)
PII_FIELD = re.compile(r"(?:^|_)(?:phone|mobile|tel|telephone|cellphone|id_?card|id_?number|email|wechat)$", re.I)


def _norm_key(key: str) -> str:
    k = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return re.sub(r"[\s\-.]+", "_", k).strip("_").lower()


def is_secret_field(key: str) -> bool:
    return bool(SENSITIVE_FIELD.search(_norm_key(key)))


def is_pii_field(key: str) -> bool:
    return bool(PII_FIELD.search(_norm_key(key)))

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{16,}"),
)

from app.agent.harness.pii import CN_ID_RE, CN_LANDLINE_RE, CN_MOBILE_RE, EMAIL_RE  # noqa: E402,F401

_MIN_SECRET_LEN = 8


def env_secret_values() -> list[str]:
    """Values of env vars whose *name* looks secret (len >= 8), longest first."""
    vals: set[str] = set()
    for name, value in os.environ.items():
        v = (value or "").strip()
        if len(v) < _MIN_SECRET_LEN or not SECRET_ENV_NAME.search(name):
            continue
        if v.lower() in ("true", "false", "disabled", "enabled"):
            continue
        vals.add(v)
    return sorted(vals, key=len, reverse=True)


def find_secrets(text: str) -> list[str]:
    """Secret substrings found in text (env values + well-known key shapes)."""
    if not text:
        return []
    hits: list[str] = []
    for v in env_secret_values():
        if v in text:
            hits.append(v)
    for pat in SECRET_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text))
    return hits


def mask_secrets(text: str, placeholder: str = "[REDACTED:secret]") -> tuple[str, int]:
    if not text:
        return text, 0
    out = text
    n = 0
    for v in env_secret_values():
        if v in out:
            n += out.count(v)
            out = out.replace(v, placeholder)
    for pat in SECRET_PATTERNS:
        out, k = pat.subn(placeholder, out)
        n += k
    return out, n


def mask_pii(text: str, keep: Iterable[str] = ()) -> str:
    """Contact PII, ID numbers, person names and addresses (backend: ``PII_BACKEND``)."""
    if not text:
        return text
    from app.agent.harness.pii import mask

    return mask(text, keep=keep)


def mask_values(text: str, values: Iterable[str], placeholder: str) -> tuple[str, int]:
    if not text:
        return text, 0
    out = text
    n = 0
    for v in sorted({str(x) for x in values if x and len(str(x)) >= 2}, key=len, reverse=True):
        if v in out:
            n += out.count(v)
            out = out.replace(v, placeholder)
    return out, n


def redact_text(
    text: Any,
    *,
    max_len: int = 0,
    extra_values: Iterable[str] = (),
) -> str:
    """Log-safe text: secrets, sandbox/extra values, then contact PII masked."""
    s = "" if text is None else str(text)
    s, _ = mask_secrets(s)
    s, _ = mask_values(s, extra_values, "[SANDBOX]")
    s = mask_pii(s)
    if max_len and len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _mask_pii_scalar(v: Any) -> str:
    s = str(v)
    masked = mask_pii(s)
    if masked != s:
        return masked
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 4:
        return digits[:3] + "****" + digits[-4:] if len(digits) >= 7 else "****"
    return "***"


def redact_obj(
    obj: Any,
    *,
    max_str: int = 300,
    max_items: int = 30,
    extra_values: Iterable[str] = (),
    _depth: int = 0,
    _key: str = "",
) -> Any:
    """Recursively redact a JSON-able structure for logs.

    Field-aware: secret-named keys become ``***``; PII-named keys and numbers
    shaped like phone / ID numbers are masked even when stored as ints.
    """
    extra = tuple(extra_values)
    if _depth > 6:
        return "…"
    kw = {"max_str": max_str, "max_items": max_items, "extra_values": extra, "_depth": _depth + 1}
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:50]:
            key = str(k)
            if is_secret_field(key):
                out[key] = "***" if v not in (None, "") else v
            elif key == "privacy_sandbox":
                out[key] = f"[{len(v) if hasattr(v, '__len__') else 0} hidden]"
            else:
                out[key] = redact_obj(v, _key=key, **kw)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, _key=_key, **kw) for v in list(obj)[:max_items]]
    if isinstance(obj, str):
        if _key and is_pii_field(_key) and obj:
            return _mask_pii_scalar(redact_text(obj, extra_values=extra))
        return redact_text(obj, max_len=max_str, extra_values=extra)
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        if _key and is_pii_field(_key):
            return _mask_pii_scalar(obj)
        if isinstance(obj, int) and mask_pii(str(obj)) != str(obj):
            return mask_pii(str(obj))
        return obj
    return redact_text(repr(obj), max_len=max_str, extra_values=extra)


_CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")


def estimate_tokens(text: Any) -> int:
    """Rough token estimate: 1 per CJK char + 1 per ~4 other chars."""
    s = "" if text is None else str(text)
    if not s:
        return 0
    cjk = len(_CJK.findall(s))
    return cjk + math.ceil((len(s) - cjk) / 4)
