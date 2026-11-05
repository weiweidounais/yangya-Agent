"""Anti-leak guardrail for anything the agent sends to a user.

Blocks / masks in outbound text:
1) secrets — env values of *KEY/TOKEN/SECRET* vars and well-known key shapes;
2) system prompts — verbatim spans of registered static prompts;
3) other users' data — contact PII / names / sandbox values owned by another user,
   plus any contact PII that is neither the current user's nor in their input;
4) privacy sandbox — values the user (or ops via ``PRIVACY_SANDBOX_VALUES``)
   marked as never-output. Masked even for the owner.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Iterable, Optional

from app.agent.harness.redact import (
    CN_ID_RE,
    CN_MOBILE_RE,
    EMAIL_RE,
    mask_secrets,
    mask_values,
)

SECRET_PLACEHOLDER = "[已隐藏]"
OTHER_USER_PLACEHOLDER = "[他人信息已隐藏]"
SANDBOX_PLACEHOLDER = "[隐私已隐藏]"
PROMPT_LEAK_REPLY = (
    "这部分属于我的内部设置，不能原样分享。不过你想了解我能帮什么，我可以直接说："
    "记住你告诉我的事、帮你写邮件和消息的草稿（你确认后才算数）、设提醒，也可以随便聊聊。"
)

_WINDOW = 16
_MIN_COVERAGE = 40

_prompts: dict[str, str] = {}
_allowed_outputs: list[str] = []
_prompt_windows: Optional[set[str]] = None
_lock = threading.Lock()


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def register_system_prompt(name: str, text: str) -> None:
    """Register a static system prompt whose verbatim content must never be output."""
    global _prompt_windows
    if not text:
        return
    with _lock:
        _prompts[name] = text
        _prompt_windows = None


def register_allowed_output(text: str) -> None:
    """Fixed user-facing text that legitimately overlaps a prompt (e.g. crisis footer)."""
    global _prompt_windows
    if not text:
        return
    with _lock:
        _allowed_outputs.append(text)
        _prompt_windows = None


def registered_prompts() -> dict[str, str]:
    return dict(_prompts)


def _windows(s: str) -> set[str]:
    n = _norm(s)
    return {n[i : i + _WINDOW] for i in range(0, max(0, len(n) - _WINDOW + 1))}


def _get_prompt_windows() -> set[str]:
    global _prompt_windows
    with _lock:
        if _prompt_windows is None:
            wins: set[str] = set()
            for p in _prompts.values():
                wins |= _windows(p)
            for a in _allowed_outputs:
                wins -= _windows(a)
            _prompt_windows = wins
        return _prompt_windows


def prompt_leak_coverage(text: str) -> int:
    """Number of output chars covered by verbatim system-prompt windows."""
    wins = _get_prompt_windows()
    n = _norm(text)
    if not wins or len(n) < _WINDOW:
        return 0
    covered = [False] * len(n)
    for i in range(0, len(n) - _WINDOW + 1):
        if n[i : i + _WINDOW] in wins:
            for j in range(i, i + _WINDOW):
                covered[j] = True
    return sum(covered)


# ---------------------------------------------------------------- user data


_DERIVED_FIELDS = ("privacy_sandbox", "dialogue_summary", "last_summary_kind")


def _profile_strings(profile: dict[str, Any]) -> str:
    """User-provided profile facts only; model-derived summaries are not "own" data."""
    p = {k: v for k, v in (profile or {}).items() if k not in _DERIVED_FIELDS}
    return json.dumps(p, ensure_ascii=False)


def sandbox_from_profile(profile: dict[str, Any]) -> list[str]:
    raw = (profile or {}).get("privacy_sandbox")
    vals: list[str] = []
    if isinstance(raw, dict):
        vals = [str(v) for v in raw.values()]
    elif isinstance(raw, (list, tuple)):
        vals = [str(v) for v in raw]
    elif isinstance(raw, str):
        vals = [raw]
    return [v.strip() for v in vals if v and len(v.strip()) >= 2]


def global_sandbox_values() -> list[str]:
    raw = os.getenv("PRIVACY_SANDBOX_VALUES") or ""
    return [v.strip() for v in re.split(r"[|\n]", raw) if len(v.strip()) >= 2]


def sandbox_values(
    user_id: str, profile: Optional[dict[str, Any]] = None, *, strict: bool = False
) -> list[str]:
    if profile is None:
        from app import db

        try:
            profile = db.get_profile(user_id) if user_id else {}
        except Exception:
            if strict:
                raise
            profile = {}
    return sandbox_from_profile(profile or {}) + global_sandbox_values()


def _pii(text: str) -> set[str]:
    out: set[str] = set()
    for pat in (EMAIL_RE, CN_MOBILE_RE, CN_ID_RE):
        for m in pat.finditer(text or ""):
            out.add(m.group(0))
    return out


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _foreign_values(text: str) -> dict[str, set[str]]:
    """Identity values present in ``text`` -> owner ids (HMAC index, every profile)."""
    from app.agent.harness import identity

    identity.sync_index()
    return identity.lookup(text)


# Unit-level markers: a home address. Street-level (company) addresses stay visible.
_RESIDENTIAL = re.compile(r"\d+\s*(?:室|单元|栋|幢|号楼)")


def _allowed_numbers() -> set[str]:
    return {"01082951332", "82951332", "12356", "120", "110"}


# ---------------------------------------------------------------- main check


def scan_output(
    text: str,
    *,
    user_id: str,
    profile: Optional[dict[str, Any]] = None,
    inputs: Iterable[str] = (),
    sandbox: Optional[Iterable[str]] = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Return {"text", "hits": [{type, count}], "blocked": bool}.

    ``user_id`` must be the authenticated owner of this turn; an empty id means
    no data is treated as "own" (strictest mode). ``sandbox`` are values already
    resolved for this request (added to the stored ones). With ``strict`` a failed
    profile / other-user lookup raises instead of scanning against empty data, so
    callers can fail closed.
    """
    original = text or ""
    if not original:
        return {"text": original, "hits": [], "blocked": False}
    hits: list[dict[str, Any]] = []
    uid = str(user_id or "")
    if profile is None:
        from app import db

        try:
            profile = db.get_profile(uid) if uid else {}
        except Exception:
            if strict:
                raise
            profile = {}
    profile = profile or {}

    out, n = mask_secrets(original, SECRET_PLACEHOLDER)
    if n:
        hits.append({"type": "secret", "count": n})

    if prompt_leak_coverage(out) >= _MIN_COVERAGE:
        hits.append({"type": "system_prompt", "count": 1})
        return {"text": PROMPT_LEAK_REPLY, "hits": hits, "blocked": True}

    from app.agent.harness.prompt_leak import similarity_leak

    sim = similarity_leak(out)
    if sim.get("leak"):
        hits.append({"type": "system_prompt", "count": 1, "method": sim.get("method", "similarity")})
        return {"text": PROMPT_LEAK_REPLY, "hits": hits, "blocked": True}

    own_sandbox = sandbox_values(uid, profile) + [str(v) for v in (sandbox or ())]
    out, n = mask_values(out, own_sandbox, SANDBOX_PLACEHOLDER)
    if n:
        hits.append({"type": "privacy_sandbox", "count": n})

    input_blob = "\n".join(str(x or "") for x in inputs)
    own_blob = _profile_strings(profile) + "\n" + input_blob
    own_emails = {v.lower() for v in EMAIL_RE.findall(own_blob)}
    own_digits = {_digits(v) for v in CN_MOBILE_RE.findall(own_blob)}

    try:
        index = _foreign_values(out)
    except Exception:
        if strict:
            raise
        index = {}
    foreign = [
        v
        for v, owners in index.items()
        if owners and uid not in owners and v not in own_blob and v in out
        and not (EMAIL_RE.fullmatch(v) and v.lower() in own_emails)
    ]
    out, n = mask_values(out, foreign, OTHER_USER_PLACEHOLDER)
    other_n = n

    from app.agent.harness.pii import detect

    own_addresses = [s.text for s in detect(own_blob, types=("address",))]
    for span in sorted(detect(out, types=("address",)), key=lambda s: -s.start):
        if not _RESIDENTIAL.search(span.text) or OTHER_USER_PLACEHOLDER in span.text:
            continue
        if span.text in own_blob or any(a in span.text or span.text in a for a in own_addresses):
            continue
        out = out[: span.start] + OTHER_USER_PLACEHOLDER + out[span.end :]
        other_n += 1

    def _mask_unowned(m: "re.Match[str]") -> str:
        nonlocal other_n
        val = m.group(0)
        d = _digits(val)
        if EMAIL_RE.fullmatch(val):
            if val.lower() in own_emails:
                return val
        elif val in own_blob or (
            CN_MOBILE_RE.fullmatch(val) and d and (d in own_digits or d in _allowed_numbers())
        ):
            return val
        other_n += 1
        return OTHER_USER_PLACEHOLDER

    for pat in (EMAIL_RE, CN_ID_RE, CN_MOBILE_RE):
        out = pat.sub(_mask_unowned, out)
    if other_n:
        hits.append({"type": "other_user_data", "count": other_n})

    return {"text": out, "hits": hits, "blocked": False}


def memory_safe(text: str, user_id: str) -> str:
    """Text about to be persisted to memory: no secrets, no sandbox values."""
    if not text:
        return text
    out, _ = mask_secrets(text, SECRET_PLACEHOLDER)
    out, _ = mask_values(out, sandbox_values(user_id), SANDBOX_PLACEHOLDER)
    return out


_PRIVATE_PROFILE_KEYS = ("phone", "email", "address", "contact")


def _strip_private_fields(profile: dict) -> dict:
    """Contact details stay in storage for trusted tools, never in the public card."""
    return {k: v for k, v in profile.items() if k not in _PRIVATE_PROFILE_KEYS and not k.startswith("_")}


def public_profile(profile: Any, *, user_id: str = "") -> Any:
    """Profile safe to return over the API: sandbox values and contact details
    never leave the server."""
    if not isinstance(profile, dict):
        return profile
    profile = _strip_private_fields(profile)
    hidden = sandbox_from_profile(profile) + global_sandbox_values()
    if user_id:
        hidden += sandbox_values(user_id)
    if not hidden and "privacy_sandbox" not in profile:
        return profile

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return mask_values(v, hidden, SANDBOX_PLACEHOLDER)[0]
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v

    out = {k: _walk(v) for k, v in profile.items() if k != "privacy_sandbox"}
    if "privacy_sandbox" in profile:
        out["privacy_sandbox"] = {"count": len(sandbox_from_profile(profile)), "values": "hidden"}
    return out
