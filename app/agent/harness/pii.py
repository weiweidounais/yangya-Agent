"""PII detection with pluggable backends and a regex fallback.

``PII_BACKEND``:
- ``regex`` (default) — structured PII (email, CN mobile, landline, ID number) plus
  context-cued Chinese person names and street-level Chinese addresses.
- ``presidio`` — Microsoft Presidio (spaCy ``PII_SPACY_MODEL``, default
  ``zh_core_web_sm``) for PERSON / LOCATION; regex still covers structured types.
- ``gliner`` — GLiNER (``GLINER_MODEL``, default ``urchade/gliner_multi_pii-v1``).
- ``auto`` — presidio, then gliner, then regex.

ML backends are optional installs (``requirements-pii.txt``). If one cannot load
or raises, detection silently falls back to regex for that call and the reason is
reported by ``backend_info()``.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Iterable, NamedTuple, Optional

log = logging.getLogger("harness.pii")


class PiiSpan(NamedTuple):
    type: str  # email | phone | id | name | address
    start: int
    end: int
    text: str
    score: float
    source: str


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
CN_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[\- ]?)?1[3-9]\d[\- ]?\d{4}[\- ]?\d{4}(?!\d)")
CN_LANDLINE_RE = re.compile(r"(?<![\d\-])0\d{2,3}-\d{7,8}(?![\d\-])")
CN_ID_RE = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")


def _surname_alt() -> str:
    from app.agent.harness.identity import COMPOUND_SURNAMES, SURNAMES

    return "(?:" + "|".join(COMPOUND_SURNAMES) + "|[" + "".join(sorted(SURNAMES)) + "])"


_NAME_PATTERNS: Optional[list[re.Pattern[str]]] = None
_ADDRESS_RE = re.compile(
    r"(?:[\u4e00-\u9fff]{2,7}(?:省|自治区))?(?:[\u4e00-\u9fff]{2,7}市)?(?:[\u4e00-\u9fff]{1,7}(?:区|县))?"
    r"[\u4e00-\u9fff0-9]{1,15}(?:路|街|大道|道|巷|弄|胡同)\d{1,5}(?:号|弄)"
    r"(?:[\u4e00-\u9fffA-Za-z0-9\-]{0,12}?(?:栋|幢|座|号楼|楼|单元|室|层))*"
    r"|[\u4e00-\u9fff]{2,12}(?:小区|花园|公寓|大厦|社区)\d{1,4}(?:栋|幢|号楼)(?:\d{1,4}(?:单元|室|层))*"
)


def _name_patterns() -> list[re.Pattern[str]]:
    global _NAME_PATTERNS
    if _NAME_PATTERNS is None:
        sn = _surname_alt()
        name = rf"(?P<n>{sn}[\u4e00-\u9fff]{{1,2}})"
        _NAME_PATTERNS = [
            re.compile(rf"(?:姓名|名字|联系人|收件人|负责人|推荐人|同事)\s*[:：]?\s*{name}(?![\u4e00-\u9fff]{{2}})"),
            re.compile(rf"(?:我叫|我是|本人)\s*{name}(?=$|[\s，,。！!；;（(])"),
            re.compile(rf"{name}(?=(?:先生|女士|小姐|同学))"),
            re.compile(rf"(?m)^\s*{name}\s*$"),
        ]
    return _NAME_PATTERNS


def _regex_detect(text: str, types: Iterable[str]) -> list[PiiSpan]:
    want = set(types)
    out: list[PiiSpan] = []

    def _all(kind: str, pat: re.Pattern[str], score: float = 1.0) -> None:
        if kind in want:
            out.extend(PiiSpan(kind, m.start(), m.end(), m.group(0), score, "regex") for m in pat.finditer(text))

    _all("email", EMAIL_RE)
    _all("id", CN_ID_RE)
    _all("phone", CN_MOBILE_RE)
    _all("phone", CN_LANDLINE_RE)
    _all("address", _ADDRESS_RE, 0.8)
    if "name" in want:
        for pat in _name_patterns():
            for m in pat.finditer(text):
                out.append(PiiSpan("name", m.start("n"), m.end("n"), m.group("n"), 0.7, "regex"))
    return out


# ---------------------------------------------------------------- ML backends

_backend_lock = threading.Lock()
_backend_state: dict[str, Any] = {"name": None, "engine": None, "error": ""}


def _load_presidio() -> Any:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    model = os.getenv("PII_SPACY_MODEL") or "zh_core_web_sm"
    provider = NlpEngineProvider(
        nlp_configuration={"nlp_engine_name": "spacy", "models": [{"lang_code": "zh", "model_name": model}]}
    )
    return AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["zh"])


def _load_gliner() -> Any:
    from gliner import GLiNER

    return GLiNER.from_pretrained(os.getenv("GLINER_MODEL") or "urchade/gliner_multi_pii-v1")


_LOADERS = {"presidio": _load_presidio, "gliner": _load_gliner}


def _configured() -> str:
    return (os.getenv("PII_BACKEND") or "regex").strip().lower()


def _engine() -> tuple[str, Any]:
    want = _configured()
    if want not in ("presidio", "gliner", "auto"):
        return "regex", None
    with _backend_lock:
        if _backend_state["name"] is not None and _backend_state.get("want") == want:
            return _backend_state["name"], _backend_state["engine"]
        order = ("presidio", "gliner") if want == "auto" else (want,)
        errors = []
        for name in order:
            try:
                eng = _LOADERS[name]()
                _backend_state.update(name=name, engine=eng, error="", want=want)
                return name, eng
            except Exception as exc:  # optional dependency / model missing
                errors.append(f"{name}: {type(exc).__name__}")
        _backend_state.update(name="regex", engine=None, error="; ".join(errors), want=want)
        log.warning("PII backend %s unavailable (%s); using regex", want, _backend_state["error"])
        return "regex", None


_PRESIDIO_MAP = {"PERSON": "name", "LOCATION": "address"}
_GLINER_LABELS = {"person": "name", "address": "address", "location": "address"}


def _min_score() -> float:
    try:
        return float(os.getenv("PII_MIN_SCORE") or 0.6)
    except ValueError:
        return 0.6


def _ml_detect(name: str, engine: Any, text: str, types: set[str]) -> list[PiiSpan]:
    out: list[PiiSpan] = []
    floor = _min_score()
    if name == "presidio":
        for r in engine.analyze(text=text, language="zh", entities=list(_PRESIDIO_MAP)):
            kind = _PRESIDIO_MAP.get(r.entity_type)
            if kind in types and r.score >= floor:
                out.append(PiiSpan(kind, r.start, r.end, text[r.start : r.end], float(r.score), name))
    elif name == "gliner":
        for e in engine.predict_entities(text, list(_GLINER_LABELS), threshold=floor):
            kind = _GLINER_LABELS.get(str(e.get("label")))
            if kind in types:
                out.append(PiiSpan(kind, int(e["start"]), int(e["end"]), e["text"], float(e.get("score") or 0), name))
    return out


ALL_TYPES = ("email", "phone", "id", "name", "address")


def detect(text: str, types: Iterable[str] = ALL_TYPES) -> list[PiiSpan]:
    """Non-overlapping PII spans (longest / highest score wins)."""
    if not text:
        return []
    want = set(types)
    spans = _regex_detect(text, want)
    name, engine = _engine()
    if engine is not None and want & {"name", "address"}:
        try:
            spans += _ml_detect(name, engine, text, want & {"name", "address"})
        except Exception as exc:
            log.warning("PII backend %s failed (%s); regex only", name, type(exc).__name__)
    spans.sort(key=lambda s: (s.start, -(s.end - s.start), -s.score))
    picked: list[PiiSpan] = []
    last_end = -1
    for s in spans:
        if s.start >= last_end and s.text.strip():
            picked.append(s)
            last_end = s.end
    return picked


_PLACEHOLDER = {"email": "[EMAIL]", "id": "[ID]", "name": "[NAME]", "address": "[ADDRESS]"}


def mask(text: str, types: Iterable[str] = ALL_TYPES, keep: Iterable[str] = ()) -> str:
    """Replace detected PII; phones keep a 3+4 digit hint. Values in ``keep`` stay."""
    if not text:
        return text
    keep_set = {k for k in keep if k}
    out, pos = [], 0
    for s in detect(text, types):
        if s.text in keep_set:
            continue
        out.append(text[pos : s.start])
        if s.type == "phone":
            d = re.sub(r"\D", "", s.text)
            if CN_MOBILE_RE.fullmatch(s.text):
                d = d[-11:]
                out.append(d[:3] + "****" + d[-4:])
            else:
                out.append(s.text[:4] + "****" + s.text[-2:])
        else:
            out.append(_PLACEHOLDER[s.type])
        pos = s.end
    out.append(text[pos:])
    return "".join(out)


def backend_info() -> dict[str, Any]:
    name, engine = _engine()
    return {
        "configured": _configured(),
        "active": name,
        "ml_loaded": engine is not None,
        "error": _backend_state.get("error") or "",
    }


def reset_backend() -> None:
    with _backend_lock:
        _backend_state.update(name=None, engine=None, error="", want=None)
