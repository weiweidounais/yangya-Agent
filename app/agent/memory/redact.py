"""Strip credentials before memory is shown or written into prompts.

Passwords, cookies, tokens and similar secrets must never appear on the memory
card. Normal words stay.
"""

from __future__ import annotations

import re

_SAFE_KEYS = {
    "name",
    "city",
    "tz",
    "occupation",
    "interests",
    "preferences",
    "facts",
    "dialogue_summary",
    "last_summary_kind",
}

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|cookie|credential|api[_-]?key|"
    r"authorization|session|jwt)",
    re.I,
)

# Credential assignments only. Do not swallow the rest of the sentence.
_SECRET_SPAN = re.compile(
    r"(?:"
    r"(?:账号|帐号|用户名|密码|口令|passwd|password|token|cookie|secret|"
    r"api[_-]?key|access[_-]?token|凭证|登录态)"
    r"\s*(?:是|为|[:=：])\s*\S+"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r")",
    re.I,
)


def is_secret_key(key: str) -> bool:
    name = str(key or "").strip()
    if not name or name in _SAFE_KEYS:
        return False
    return bool(_SECRET_KEY.search(name))


def redact_text(text: str) -> str:
    raw = str(text or "")
    if not raw:
        return ""
    cleaned = _SECRET_SPAN.sub("", raw)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([，。；、])", r"\1", cleaned)
    return cleaned.strip()


def display_text(text: str) -> str:
    """Redacted text for plain memory rows, without markdown emphasis."""
    raw = str(text or "").strip()
    cleaned = redact_text(raw)
    # Chat bubbles render markdown; memory rows are text. Also clean legacy
    # rows on read. Remove strong markers even when truncation cut the pair.
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"(?<!\w)\*([^*\n]+)\*", r"\1", cleaned)
    return cleaned
