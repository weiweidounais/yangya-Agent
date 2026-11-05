"""Detect and redact credentials / PII typed into chat before it reaches the graph.

Anything matched here is replaced before the message is checkpointed, sent to an
LLM, written to memory or logged; the user is pointed at the secret sandbox instead.
"""

from __future__ import annotations

import re

from app.secrets_vault import CATALOG

_ID_CARD = re.compile(
    r"(?<![0-9A-Za-z])[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])"
)
_PHONE = re.compile(r"(?<![\d+])(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?!\d)")
_PASSWORD = re.compile(
    r"(密码|口令|验证码|password|passwd|pwd)([^\S\r\n]*(?:是|为|[:：=])[^\S\r\n]*)([^\r\n]+)", re.I
)
_COOKIE = re.compile(
    r"(cookie)([^\S\r\n]*(?:(?:是|为|[:：=])[^\S\r\n]*|(?=[^\s，。]{16,})))([^\r\n]+)", re.I
)
_COOKIE_PAIRS = re.compile(
    r"(?:[A-Za-z_][\w-]{1,40}=[^;\s]{6,};\s*){2,}[A-Za-z_][\w-]{1,40}=[^;\s]{6,};?"
)

HIDDEN_ID = "[身份证号已隐藏]"
HIDDEN_PHONE = "[手机号已隐藏]"
HIDDEN = "[已隐藏]"

CREDENTIAL_KINDS = ("password", "cookie")


def redact_sensitive(text: str, *, include_contact: bool = True) -> tuple[str, list[str]]:
    """Return (redacted_text, kinds_found). ``include_contact=False`` skips phone / ID."""
    out = str(text or "")
    kinds: list[str] = []

    def _hit(kind: str) -> None:
        if kind not in kinds:
            kinds.append(kind)

    def _sub_kv(kind: str):
        def repl(m: "re.Match[str]") -> str:
            _hit(kind)
            return f"{m.group(1)}{m.group(2)}{HIDDEN}"

        return repl

    out = _PASSWORD.sub(_sub_kv("password"), out)
    out = _COOKIE.sub(_sub_kv("cookie"), out)

    def _pairs(m: "re.Match[str]") -> str:
        _hit("cookie")
        return HIDDEN

    out = _COOKIE_PAIRS.sub(_pairs, out)

    if include_contact:

        def _id(m: "re.Match[str]") -> str:
            _hit("id_card")
            return HIDDEN_ID

        def _phone(m: "re.Match[str]") -> str:
            _hit("phone")
            return HIDDEN_PHONE

        out = _ID_CARD.sub(_id, out)
        out = _PHONE.sub(_phone, out)
    return out, kinds


def platform_key_in(text: str) -> str:
    low = str(text or "").lower()
    for key, spec in CATALOG.items():
        if spec["kind"] != "login":
            continue
        if any(alias.lower() in low for alias in spec["aliases"]):
            return key
    return ""


def suggest_secret_key(redacted_text: str, kinds: list[str]) -> str:
    """Which sandbox to open for what the user just tried to paste."""
    if any(k in kinds for k in CREDENTIAL_KINDS):
        return platform_key_in(redacted_text) or "account_login"
    if "id_card" in kinds:
        return "id_card"
    platform = platform_key_in(redacted_text)
    if platform and re.search(r"(账号|帐号|登录|登陆)", redacted_text):
        return platform
    return "phone"


def privacy_reply(key: str) -> str:
    label = CATALOG.get(key, {}).get("label", "这类信息")
    return (
        "先停一下：密码、Cookie、手机号、身份证号这类信息别直接发在聊天里，我已经把它隐藏了，没有读取，也不会记下来。"
        f"如果确实需要，点下面的安全输入区保存「{label}」，内容会加密存放，聊天记录和 AI 都看不到。"
    )
