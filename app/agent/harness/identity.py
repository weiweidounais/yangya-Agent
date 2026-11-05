"""Pseudonymous ids (HMAC) and the cross-user identity index used by the leak guard.

- ``pseudonym(value)`` — stable keyed HMAC used for user / thread ids in traces and
  in the Langfuse export, so neither store holds raw ids. Key: ``HARNESS_ID_HMAC_KEY``
  (>= 16 chars) or a random key generated once at ``DATA_DIR/.harness_id_key``.
- identity index — one row per (HMAC(value), owner) for every identifying value of
  every profile (contact PII, names incl. common 2-char names, addresses, sandbox
  tokens). Kept in sync on ``db.save_profile`` plus a batched, uncapped backfill.
  Lookups hash candidate substrings of an output, so plaintext PII is never
  duplicated into the index.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, Integer, String, delete, func, select
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UserProfile, init_db
from app.data_access.session import memory_session

log = logging.getLogger("harness.identity")

_MIN_KEY = 16
_key_lock = threading.Lock()
_key_cache: dict[str, bytes] = {}

# Common Chinese surnames: a 2-char name is indexed only when it starts with one,
# which keeps everyday 2-char words out of the index.
COMPOUND_SURNAMES = (
    "欧阳", "司马", "诸葛", "上官", "东方", "皇甫", "尉迟", "公孙", "慕容", "令狐",
    "长孙", "宇文", "司徒", "夏侯", "轩辕", "端木", "独孤", "南宫", "西门", "百里",
)
SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈"
    "姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
    "常温康施文牛樊葛邢安齐易乔伍庞颜倪庄聂章鲁岳翟殷詹申欧耿关兰焦俞左柳甘祝包宁尚符舒阮柯纪梅童凌毕单季裴"
    "霍涂成苗谷盛曲翁冉骆蓝路游辛靳管柴蒙鲍华喻祁蒲房滕屈饶解牟艾尤阳时穆农司卓古吉缪简车项连芦麦褚娄窦戚岑"
    "景党宫费卜冷晏席卫米柏宗瞿桂全佟应臧闵苟邬边卞姬师和仇栾隋商刁沙荣巫寇桑郎甄丛仲虞敖巩明佘池查麻苑迟邝"
)


def _key_file() -> Path:
    from app.db import DATA_DIR

    return Path(DATA_DIR) / ".harness_id_key"


def _hmac_key() -> bytes:
    env = (os.getenv("HARNESS_ID_HMAC_KEY") or "").strip()
    if len(env) >= _MIN_KEY:
        return env.encode()
    path = _key_file()
    cached = _key_cache.get(str(path))
    if cached:
        return cached
    with _key_lock:
        if str(path) in _key_cache:
            return _key_cache[str(path)]
        try:
            key = path.read_bytes().strip()
        except OSError:
            key = b""
        if len(key) < _MIN_KEY:
            key = secrets.token_hex(32).encode()
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(key)
        _key_cache[str(path)] = key
        return key


def _digest(value: str) -> str:
    return hmac.new(_hmac_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()


def pseudonym(value: Any, prefix: str = "u") -> str:
    """Stable keyed pseudonym; '' stays ''. Already-pseudonymised values pass through."""
    s = str(value or "").strip()
    if not s:
        return ""
    if re.fullmatch(rf"{prefix}_[0-9a-f]{{24}}", s):
        return s
    return f"{prefix}_{_digest(prefix + ':' + s)[:24]}"


def user_pseudonym(user_id: Any) -> str:
    return pseudonym(user_id, "u")


def thread_pseudonym(thread_id: Any) -> str:
    return pseudonym(thread_id, "t")


# ---------------------------------------------------------------- identity index


class IdentityIndex(Base):
    __tablename__ = "identity_index"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    value_hash: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="")


class IdentityIndexed(Base):
    __tablename__ = "identity_indexed"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\- ]?)?1[3-9]\d[\- ]?\d{4}[\- ]?\d{4}(?!\d)")
_IDNO = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff·]{2,}")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-.]{3,63}")
_MAX_NGRAM = 8


def _norm_value(kind: str, v: str) -> str:
    v = (v or "").strip()
    if kind == "phone":
        return re.sub(r"\D", "", v)[-11:]
    if kind == "email":
        return v.lower()
    if kind == "id":
        return v.upper()
    return v


_TEXT_KINDS = ("name", "sandbox", "address")


def _hash(kind: str, v: str) -> str:
    space = "text" if kind in _TEXT_KINDS else kind
    return _digest(f"idx:{space}:{_norm_value(kind, v)}")


def is_name_like(name: str) -> bool:
    n = (name or "").strip()
    if re.fullmatch(r"[\u4e00-\u9fff·]{3,8}", n):
        return True
    if re.fullmatch(r"[\u4e00-\u9fff]{2}", n):
        return n[0] in SURNAMES or n in COMPOUND_SURNAMES
    return len(n) >= 5 and " " in n and n.replace(" ", "").isalpha()


def identity_values(profile: dict[str, Any]) -> set[tuple[str, str]]:
    """(kind, value) pairs that identify the owner of ``profile``."""
    from app.agent.harness.leak import sandbox_from_profile
    from app.agent.harness.pii import detect

    p = {k: v for k, v in (profile or {}).items() if k not in ("dialogue_summary", "last_summary_kind")}
    blob = json.dumps({k: v for k, v in p.items() if k != "privacy_sandbox"}, ensure_ascii=False)
    out: set[tuple[str, str]] = set()
    out |= {("email", m.group(0)) for m in _EMAIL.finditer(blob)}
    out |= {("phone", m.group(0)) for m in _PHONE.finditer(blob)}
    out |= {("id", m.group(0)) for m in _IDNO.finditer(blob)}
    name = str(p.get("name") or "").strip()
    if is_name_like(name):
        out.add(("name", name))
    raw = "\n".join(str(p.get(k) or "") for k in ("address", "contact"))
    for span in detect(raw, types=("name", "address")):
        if span.type == "name" and is_name_like(span.text):
            out.add(("name", span.text))
        elif span.type == "address" and len(span.text) >= 6:
            out.add(("address", span.text))
    for v in sandbox_from_profile(p):
        out.add(("sandbox", v))
    return {(k, v) for k, v in out if len(_norm_value(k, v)) >= 2}


def index_profile(user_id: str, profile: dict[str, Any], *, updated_at: Optional[datetime] = None) -> int:
    init_db()
    rows = [
        IdentityIndex(value_hash=_hash(kind, v), user_id=str(user_id), kind=kind)
        for kind, v in identity_values(profile)
    ]
    now = datetime.now(timezone.utc)
    with memory_session() as s:
        s.execute(delete(IdentityIndex).where(IdentityIndex.user_id == str(user_id)))
        s.add_all(rows)
        mark = s.get(IdentityIndexed, str(user_id))
        if mark is None:
            s.add(IdentityIndexed(user_id=str(user_id), indexed_at=now, profile_updated_at=updated_at or now))
        else:
            mark.indexed_at = now
            mark.profile_updated_at = updated_at or now
        s.flush()
    return len(rows)


_sync_state: dict[str, Any] = {"at": 0.0, "engine": None, "scope": None}
_sync_lock = threading.Lock()


def sync_index(*, batch: int = 500, force: bool = False, min_interval: float = 30.0) -> int:
    """Backfill profiles visible to the trusted identity (no cap; batched)."""
    from app.db import ENGINE
    from app.data_access import current_user, IdentityRequired

    try:
        scope = current_user()
    except IdentityRequired:
        # Anonymous output can still be checked against the existing HMAC index,
        # but cannot backfill private profiles. Cache warmth must not decide this.
        return 0

    now = time.monotonic()
    if (not force and _sync_state["engine"] is ENGINE and _sync_state["scope"] == scope
            and now - _sync_state["at"] < min_interval):
        return 0
    with _sync_lock:
        init_db()
        done = 0
        while True:
            with memory_session() as s:
                q = (
                    select(UserProfile.user_id, UserProfile.profile_json, UserProfile.updated_at)
                    .outerjoin(IdentityIndexed, IdentityIndexed.user_id == UserProfile.user_id)
                    .where(
                        (IdentityIndexed.user_id.is_(None))
                        | (UserProfile.updated_at > IdentityIndexed.profile_updated_at)
                    )
                    .limit(batch)
                )
                pending = s.execute(q).all()
            if not pending:
                break
            for uid, pj, upd in pending:
                try:
                    prof = json.loads(pj or "{}")
                except json.JSONDecodeError:
                    prof = {}
                index_profile(uid, prof if isinstance(prof, dict) else {}, updated_at=upd)
                done += 1
        from app.db import after_memory_commit

        after_memory_commit(lambda: _sync_state.update(at=time.monotonic(), engine=ENGINE, scope=scope))
        return done


def _name_context_ok(text: str, start: int, end: int) -> bool:
    """2-char names only count with a name-like boundary / cue (limits word collisions)."""
    before = text[max(0, start - 4) : start]
    after = text[end : end + 4]
    if not re.match(r"[\u4e00-\u9fff]", text[start - 1 : start] or " ") or not re.match(
        r"[\u4e00-\u9fff]", text[end : end + 1] or " "
    ):
        return True
    if re.search(r"(姓名|名字|联系人|同事|我叫|我是|叫|和|跟|给|找|问|是|：|:)$", before):
        return True
    return bool(re.match(r"(先生|女士|小姐|同学|老师|经理|的资料|的电话|的邮箱|的手机|说|在|，|,|。)", after))


def candidates(text: str) -> dict[str, tuple[str, str]]:
    """hash -> (kind, original substring) for every value of ``text`` that could be indexed."""
    out: dict[str, tuple[str, str]] = {}

    def _add(kind: str, v: str) -> None:
        out.setdefault(_hash(kind, v), (kind, v))

    for m in _EMAIL.finditer(text or ""):
        _add("email", m.group(0))
    for m in _PHONE.finditer(text or ""):
        _add("phone", m.group(0))
    for m in _IDNO.finditer(text or ""):
        _add("id", m.group(0))
    for m in _TOKEN.finditer(text or ""):
        _add("sandbox", m.group(0))
    from app.agent.harness.pii import detect

    for span in detect(text or "", types=("address",)):
        _add("address", span.text)
    for m in _CJK_RUN.finditer(text or ""):
        run, base = m.group(0), m.start()
        for i in range(len(run)):
            for n in range(2, min(_MAX_NGRAM, len(run) - i) + 1):
                sub = run[i : i + n]
                if n == 2 and not _name_context_ok(text, base + i, base + i + 2):
                    continue
                _add("name", sub)
    return out


def lookup(text: str) -> dict[str, set[str]]:
    """original substring -> owner user_ids, for indexed identity values found in ``text``."""
    cands = candidates(text)
    if not cands:
        return {}
    found: dict[str, set[str]] = {}
    keys = list(cands)
    with memory_session() as s:
        for i in range(0, len(keys), 900):
            chunk = keys[i : i + 900]
            for h, uid in s.execute(
                select(IdentityIndex.value_hash, IdentityIndex.user_id).where(IdentityIndex.value_hash.in_(chunk))
            ):
                found.setdefault(cands[h][1], set()).add(str(uid))
    return found


def index_stats() -> dict[str, Any]:
    init_db()
    with memory_session() as s:
        return {
            "values": int(s.execute(select(func.count(IdentityIndex.id))).scalar() or 0),
            "users": int(s.execute(select(func.count(IdentityIndexed.user_id))).scalar() or 0),
            "profiles": int(s.execute(select(func.count(UserProfile.user_id))).scalar() or 0),
        }


def drop_user(user_id: str) -> None:
    with memory_session() as s:
        s.execute(delete(IdentityIndex).where(IdentityIndex.user_id == str(user_id)))
        s.execute(delete(IdentityIndexed).where(IdentityIndexed.user_id == str(user_id)))
        s.flush()

