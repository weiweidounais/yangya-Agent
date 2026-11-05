"""No-training SiliconFlow exemplar kNN. Scores are evidence scores, not probabilities."""
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import threading

LABELS = ("confirm", "chat", "unresolved", "crisis")
EXEMPLARS = Path(__file__).resolve().parents[2] / "intents/exemplars.yaml"
_LOCK = threading.RLock()
EMBEDDING_BASE_URL = "https://api.siliconflow.cn/v1"
EMBEDDING_MODEL = "Pro/BAAI/bge-m3"
VECTOR_CACHE = EXEMPLARS.with_name("exemplar-vectors.json")


def cache_metadata():
    return {"model": EMBEDDING_MODEL, "base_url": EMBEDDING_BASE_URL,
            "exemplars_sha256": hashlib.sha256(EXEMPLARS.read_bytes()).hexdigest(),
            "exemplar_version": exemplars()["version"], "status": "DRAFT"}


def unit_interval(value):
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid policy value")
    return value


def thresholds():
    low = unit_interval(os.getenv("INTENT_SCORE_FALLBACK_THRESHOLD", ".55"))
    high = unit_interval(os.getenv("INTENT_SCORE_ROUTE_THRESHOLD", ".85"))
    if low >= high:
        raise ValueError("inverted thresholds")
    return low, high


@lru_cache(maxsize=1)
def exemplars():
    # JSON is a YAML subset, avoiding another runtime parser dependency.
    data = json.loads(EXEMPLARS.read_text())
    if set(data["intents"]) != set(LABELS):
        raise ValueError("invalid exemplar labels")
    if any(not 10 <= len(rows) <= 30 for rows in data["intents"].values()):
        raise ValueError("expected 10-30 exemplars per intent")
    return data


def _normalize(vectors):
    import numpy as np
    vectors = np.asarray(vectors, dtype=np.float32)
    if vectors.ndim != 2 or not np.isfinite(vectors).all():
        raise ValueError("invalid embeddings")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if (norms <= 0).any():
        raise ValueError("zero embedding")
    return vectors / norms


class HostedEncoder:
    def encode(self, texts):
        from app.gateway.llm_gateway import embed_texts

        rows = []
        for start in range(0, len(texts), 10):
            rows.extend(embed_texts(texts[start:start + 10],
                                   model=EMBEDDING_MODEL, base_url=EMBEDDING_BASE_URL))
        return _normalize(rows)


def build_vector_cache():
    """Explicit refresh: encode every draft exemplar with the serving API model."""
    data = exemplars()
    texts = [text for rows in data["intents"].values() for text in rows]
    vectors = HostedEncoder().encode(texts)
    payload = {**cache_metadata(), "vectors": vectors.tolist()}
    temporary = VECTOR_CACHE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    temporary.replace(VECTOR_CACHE)
    _load.cache_clear()
    return vectors.shape


@lru_cache(maxsize=1)
def _load():
    # Cache failed initialization too; restart after repairing cache/credentials.
    try:
        encoder = HostedEncoder()
        data = exemplars()
        labels = [label for label, rows in data["intents"].items() for _ in rows]
        cached = json.loads(VECTOR_CACHE.read_text())
        if any(cached.get(k) != v for k, v in cache_metadata().items()):
            raise ValueError("stale exemplar cache; rebuild with SiliconFlow")
        vectors = _normalize(cached["vectors"])
        if len(vectors) != len(labels):
            raise ValueError("invalid exemplar cache size")
        return encoder, vectors, labels, None
    except Exception as exc:
        return None, None, None, type(exc).__name__


def _resource():
    with _LOCK:
        encoder, vectors, labels, error = _load()
    if error:
        raise RuntimeError("embedding initialization failed: " + error)
    return EMBEDDING_MODEL, encoder, vectors, labels


def warmup():
    """Load cached reference vectors only; default rules never initialize scoring."""
    if os.getenv("INTENT_SCORING_MODE", "rules") == "hybrid":
        try:
            _resource()
        except Exception:
            pass


def score_text(text):
    if not text.strip():
        raise ValueError("empty input")
    model, encoder, vectors, labels = _resource()
    query = encoder.encode([text])[0]
    similarities = vectors @ query
    k = int(os.getenv("INTENT_KNN_K", "3"))
    if not 1 <= k <= 10:
        raise ValueError("invalid k")
    scores = {}
    for label in LABELS:
        top = sorted((float(s) for s, owner in zip(similarities, labels) if owner == label), reverse=True)[:k]
        scores[label] = sum(top) / len(top)
    ranked = sorted(scores, key=scores.get, reverse=True)
    margin = max(0., scores[ranked[0]] - scores[ranked[1]])
    # Absolute support plus separation, capped by separation evidence.
    # A tie must never be confident just because both exemplars are very similar.
    confidence = max(0., min(1., scores[ranked[0]] + margin, margin / .1))
    return scores, {"confidence": confidence, "margin": margin, "winner": ranked[0],
                    "model_version": model, "exemplar_version": exemplars()["version"],
                    "hosted": True}


def second_opinion(text, expected):
    from app.gateway.llm_gateway import classify_label
    result = classify_label(text, LABELS)
    if result is None:
        return None
    label, probability = result
    return label == expected and probability >= unit_interval(os.getenv("INTENT_LLM_THRESHOLD", ".95"))
