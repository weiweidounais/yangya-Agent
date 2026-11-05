"""Non-verbatim system-prompt leak detection (paraphrase / translation).

Complements the verbatim 16-char window check in ``leak.py``:

A. paraphrase — char-bigram containment between output sentences and the
   sentences of each registered prompt; several *distinct* prompt sentences
   re-stated in one reply is a leak.
B. cross-lingual concepts — a curated zh/en concept lexicon; a reply covering
   many of a prompt's distinctive behavioural concepts (in either language) is a
   leak. This is what catches an English translation without embeddings.
C. optional embeddings — ``PROMPT_LEAK_EMBEDDINGS=sentence-transformers`` (model
   ``PROMPT_LEAK_MODEL``, default ``paraphrase-multilingual-MiniLM-L12-v2``) for
   multilingual sentence similarity.

A and B only fire together with *disclosure framing* (规则/设定/提示词/instructions/
"I was told" …) unless the overlap is overwhelming, so a reply that merely
*follows* the persona (warm, empathetic, offers a next step) is not a leak.
Detection is heuristic: it raises the bar for prompt extraction, it does not
guarantee that every rewording is caught.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any, Callable, Optional

log = logging.getLogger("harness.prompt_leak")

# (zh pattern, en pattern, weight). Weight 2 = behavioural / meta concept that a
# normal reply would not spell out; weight 1 = capability / topic words.
CONCEPTS: tuple[tuple[str, str, int], ...] = (
    (r"个人助理|小助", r"personal assistant", 2),
    (r"温暖|靠谱的朋友|身边.{0,4}朋友", r"warm|reliable friend|like a friend", 2),
    (r"口语化?|像朋友发消息", r"colloquial|conversational|casual(ly)?|texting", 2),
    (r"客服|说明书", r"customer service|support agent|manual|instruction booklet", 2),
    (r"共情|接住情绪", r"empath|acknowledge (their |the )?(feelings|emotions)|validate", 2),
    (r"说教|讲道理", r"lectur|preach|moraliz", 2),
    (r"别难过|空话", r"don'?t be sad|empty (words|platitudes)|platitude", 2),
    (r"不拒绝|只能帮你做", r"never refuse|only help (you )?with", 2),
    (r"没有身体|亲身经历|看过、吃过|去过|亲身感受", r"no (physical )?body|first-?hand|personal experience|never (say|claim) (you|i) (have )?(eaten|seen|been)", 2),
    (r"像真人|我也经历过|我特别懂", r"sound(ing)? (like )?(a )?human|i'?ve been through|i totally understand", 2),
    (r"绝不编造|不编造|编造", r"fabricat|make up|invent (the user'?s )?(experience|data)", 2),
    (r"擅自外发|对外发消息", r"send (messages|emails) (on|for)|submit (forms )?without", 2),
    (r"不假装真人|掌握决定权|让用户.{0,4}决定", r"pretend to be (a )?human|decision (stays|rests) with (the )?user|user (keeps|stays) in control", 2),
    (r"实时信息|查不到", r"real-?time (info|information|data)|can'?t look (it )?up", 2),
    (r"篇幅随对方|少用列表|少用表格", r"(match|mirror) (their|the user'?s) length|avoid (lists|tables|bullet)", 2),
    (r"最多一个|轻轻给", r"at most one|one small next step|gently (offer|suggest)", 2),
    (r"心理安全分类器|分类器", r"classifier", 2),
    (r"宁可误判|漏判", r"false (positive|negative)|err on the side", 2),
    (r"只输出\s*(YES|NO|合法 JSON)", r"only (output|answer|reply) (with )?(yes|no|json)", 2),
    (r"不评判", r"non-?judg|without judg|don'?t judge", 1),
    (r"热线", r"hotline", 1),
    (r"确认卡|用户.{0,4}确认", r"confirm(ation)? card|user (must )?confirm", 1),
    (r"确认卡", r"confirm(ation)? card", 1),
    (r"草稿", r"draft", 1),
    (r"提醒", r"reminder", 1),
    (r"档案", r"profile", 1),
    (r"焦虑|被拒|低落", r"anxi|reject|feeling (down|low)", 1),
)

_FRAMING = re.compile(
    r"(规则|原则|设定|指令|提示词|系统提示|内部设置|我被要求|被设定|人设|要求我|被告知|需要遵守|准则|我的配置|"
    r"system\s*prompt|instructions?|rules?\b|guidelines?|principles?|i\s*(was|am|have been)\s*(told|instructed|asked|configured)|"
    r"my\s*(prompt|configuration|setup|directives?))",
    re.I,
)
_NUMBERED = re.compile(r"(?m)^\s*(?:\d+[a-z]?[.、)）]|[-•*])\s+\S")
_SENT_SPLIT = re.compile(r"[。；;！!？?\n]+|(?<=[.])\s+")

_PARA_MIN_CONTAIN = 0.55
_PARA_MIN_SHARED = 6

_lock = threading.Lock()
_cache: dict[str, Any] = {"key": None, "prompts": {}}


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", (s or "").lower())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _sentences(text: str) -> list[str]:
    out = []
    for raw in _SENT_SPLIT.split(text or ""):
        s = re.sub(r"^\s*(?:\d+[a-z]?[.、)）]|[-•*])\s*", "", raw).strip()
        if len(re.sub(r"\s+", "", s)) >= 8:
            out.append(s)
    return out


def _concepts(text: str, lang_both: bool = True) -> set[int]:
    found = set()
    for i, (zh, en, _w) in enumerate(CONCEPTS):
        if re.search(zh, text or "", re.I) or (lang_both and re.search(en, text or "", re.I)):
            found.add(i)
    return found


def _prepared() -> dict[str, dict[str, Any]]:
    from app.agent.harness.leak import _allowed_outputs, registered_prompts

    prompts = registered_prompts()
    key = (tuple(sorted((k, hash(v)) for k, v in prompts.items())), len(_allowed_outputs))
    with _lock:
        if _cache["key"] == key:
            return _cache["prompts"]
        allowed = "\n".join(_allowed_outputs)
        prepared: dict[str, dict[str, Any]] = {}
        for name, text in prompts.items():
            sents = [s for s in _sentences(text) if s not in allowed]
            prepared[name] = {
                "sentences": sents,
                "bigrams": [_bigrams(s) for s in sents],
                "concepts": _concepts(text, lang_both=False),
            }
        _cache.update(key=key, prompts=prepared)
        return prepared


# ---------------------------------------------------------------- embeddings (optional)

Encoder = Callable[[list[str]], Any]
_encoder_state: dict[str, Any] = {"enc": None, "loaded": False, "error": ""}


def _encoder() -> Optional[Encoder]:
    if _encoder_state.get("custom"):
        return _encoder_state["enc"]
    if (os.getenv("PROMPT_LEAK_EMBEDDINGS") or "").strip().lower() not in ("sentence-transformers", "st", "1"):
        return None
    with _lock:
        if not _encoder_state["loaded"]:
            _encoder_state["loaded"] = True
            try:
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(os.getenv("PROMPT_LEAK_MODEL") or "paraphrase-multilingual-MiniLM-L12-v2")
                _encoder_state["enc"] = lambda xs: model.encode(xs, normalize_embeddings=True)
            except Exception as exc:
                _encoder_state["error"] = type(exc).__name__
                log.warning("prompt-leak embeddings unavailable (%s)", type(exc).__name__)
        return _encoder_state["enc"]


def set_encoder(enc: Optional[Encoder]) -> None:
    """Test seam / custom embedding backend (vectors must be L2-normalised)."""
    with _lock:
        _encoder_state.update(enc=enc, loaded=enc is not None, custom=enc is not None, error="")


def _embedding_matches(out_sents: list[str], prompt_sents: list[str], enc: Encoder, threshold: float) -> int:
    if not out_sents or not prompt_sents:
        return 0
    a = enc(out_sents)
    b = enc(prompt_sents)
    matched = set()
    for va in a:
        best, idx = -1.0, -1
        for j, vb in enumerate(b):
            sim = float(sum(x * y for x, y in zip(va, vb)))
            if sim > best:
                best, idx = sim, j
        if best >= threshold:
            matched.add(idx)
    return len(matched)


# ---------------------------------------------------------------- main check


def similarity_leak(text: str) -> dict[str, Any]:
    """{"leak": bool, "prompt": name, "method": ..., "paraphrased": n, "concepts": n}."""
    res: dict[str, Any] = {"leak": False, "prompt": "", "method": "", "paraphrased": 0, "concepts": 0}
    if not text or len(text) < 30:
        return res
    framed = bool(_FRAMING.search(text))
    listed = len(_NUMBERED.findall(text)) >= 3
    out_sents = _sentences(text)
    out_grams = [_bigrams(s) for s in out_sents]
    out_concepts = _concepts(text)
    enc = _encoder()
    try:
        emb_threshold = float(os.getenv("PROMPT_LEAK_EMB_THRESHOLD") or 0.8)
    except ValueError:
        emb_threshold = 0.8

    for name, p in _prepared().items():
        matched: set[int] = set()
        for g in out_grams:
            if len(g) < _PARA_MIN_SHARED:
                continue
            for j, pg in enumerate(p["bigrams"]):
                shared = len(g & pg)
                if shared >= _PARA_MIN_SHARED and shared / len(g) >= _PARA_MIN_CONTAIN:
                    matched.add(j)
        n_sent = max(1, len(p["sentences"]))
        para_hit = len(matched) >= 3 and (framed or len(matched) >= max(5, n_sent // 2))

        pc = p["concepts"]
        cover = pc & out_concepts
        weight = sum(CONCEPTS[i][2] for i in cover)
        strong = sum(1 for i in cover if CONCEPTS[i][2] == 2)
        need_strong = max(3, (sum(1 for i in pc if CONCEPTS[i][2] == 2) + 1) // 2)
        concept_hit = bool(pc) and strong >= need_strong and weight >= 8 and (framed or (listed and strong >= need_strong + 2))

        emb_hit = False
        emb_n = 0
        if enc is not None and (framed or listed):
            try:
                emb_n = _embedding_matches(out_sents, p["sentences"], enc, emb_threshold)
                emb_hit = emb_n >= 3
            except Exception as exc:
                log.warning("embedding similarity failed (%s)", type(exc).__name__)

        if para_hit or concept_hit or emb_hit:
            return {
                "leak": True,
                "prompt": name,
                "method": "paraphrase" if para_hit else ("concepts" if concept_hit else "embedding"),
                "paraphrased": len(matched),
                "concepts": strong,
                "embedding_matches": emb_n,
            }
        if len(matched) > res["paraphrased"] or strong > res["concepts"]:
            res.update(prompt=name, paraphrased=len(matched), concepts=strong)
    return res
