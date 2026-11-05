"""Privacy guard for IM channels.

Rules:
  * Private data (phone, ID card, email, bank card) is never
    collected in a channel. The user gets a one-time link to the web sandbox.
  * Inbound PII is redacted before it reaches logs, the agent, or HTTP echoes.
  * Outbound text is redacted too, and any reply that asks for contact details or
    contact details gets the sandbox link appended.
"""

from __future__ import annotations

import re
from typing import Any

# Crisis hotlines appended by the chat persona must survive redaction.
_ALLOWED_NUMBERS = {"01082951332", "82951332", "12356", "120", "110"}

_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("id_card", "身份证号", re.compile(r"(?<![0-9A-Za-z])\d{17}[\dXx](?![0-9A-Za-z])")),
    ("bank_card", "银行卡号", re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")),
    ("mobile", "手机号", re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")),
    ("email", "邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]

_VERB = r"(发|贴|粘贴|上传|提供|告诉|留下?|填写?|补充?)"
_PRIVATE_NOUN = r"(手机|电话|号码|邮箱|身份证|银行卡|住址|联系方式)"
_ASKS_FOR_PRIVATE = re.compile(
    rf"{_VERB}[^。！？\n]{{0,8}}{_PRIVATE_NOUN}|{_PRIVATE_NOUN}[^。！？\n]{{0,4}}{_VERB}"
)

_LABELS = {kind: label for kind, label, _ in _PATTERNS}


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _is_hit(kind: str, match: str) -> bool:
    digits = re.sub(r"\D", "", match)
    if digits in _ALLOWED_NUMBERS:
        return False
    if kind == "bank_card":
        return 16 <= len(digits) <= 19 and _luhn_ok(digits)
    return True


def detect_pii(text: str) -> list[str]:
    """Return PII kinds found in text (stable order, de-duplicated)."""
    found: list[str] = []
    for kind, _, pattern in _PATTERNS:
        if kind in found:
            continue
        if any(_is_hit(kind, m.group(0)) for m in pattern.finditer(text or "")):
            found.append(kind)
    return found


def redact(text: str) -> str:
    out = text or ""
    for kind, label, pattern in _PATTERNS:
        out = pattern.sub(
            lambda m, k=kind, lb=label: f"[{lb}已隐去]" if _is_hit(k, m.group(0)) else m.group(0),
            out,
        )
    return out


def describe(kinds: list[str]) -> str:
    return "、".join(_LABELS.get(k, k) for k in kinds) or "隐私信息"


def asks_for_private_info(text: str) -> bool:
    return bool(_ASKS_FOR_PRIVATE.search(text or ""))


def redact_inbound(inbound: dict[str, Any]) -> dict[str, Any]:
    """Copy of an inbound dict safe to echo/log: redacted text, no raw payload."""
    from app.privacy import redact_sensitive

    safe = {k: v for k, v in (inbound or {}).items() if k != "raw"}
    safe["text"] = redact(redact_sensitive(str(safe.get("text") or ""))[0])
    return safe
