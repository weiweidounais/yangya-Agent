"""Offline synthetic A/B using the production DAL and retrieval functions.

Run: python -m scripts.eval_memory_retrieval --mode mock --output /tmp/memory-ab.json
The toy semantic oracle recognizes a fixed synonym vocabulary, not question IDs.
This regression check measures retrieval plumbing, not real model quality.
"""
import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy import create_engine

from app import db
from app.agent.memory import episodic
from app.agent.memory import retrieval
from app.data_access import memory_facts, user_scope
from app.data_access.session import make_session_factory
from app.gateway import embeddings, providers, reranking


# One stored sentence and four independent paraphrases per topic.
TOPICS = [
    ("I prefer working from home", ("remote", "work from home", "telecommute", "home office")),
    ("I avoid late-night calls", ("late-night", "evening calls", "after dark phone", "night time rings")),
    ("I write Python scripts", ("python", "scripting language", "dynamic language", "py scripts")),
    ("I run on weekends", ("running", "jogging", "morning run", "weekend jog")),
    ("I live in Shanghai", ("shanghai", "沪上", "魔都", "上海")),
    ("I need flexible hours", ("flexible", "adjustable schedule", "choose my hours", "variable start time")),
    ("I mentor my younger cousin", ("mentor", "coach my cousin", "teach a relative", "guide a beginner")),
    ("I am learning photography", ("photography", "camera skills", "taking pictures", "photo lessons")),
    ("I prefer small gatherings", ("small gatherings", "compact group", "few friends", "tiny party")),
    ("I travel to Hangzhou next week", ("hangzhou", "business trip", "next week travel", "西湖")),
    ("I avoid overtime", ("overtime", "late nights", "weekend shifts", "after hours")),
    ("I enjoy cooking at home", ("cooking", "home kitchen", "making dinner", "recipes")),
]


def semantic_vector(text):
    text = text.lower()
    values = [float(any(term in text for term in aliases)) for _, aliases in TOPICS]
    return values + [1.0] + [0.0] * (embeddings.DIMENSIONS - len(values) - 1)


def fake_embed(text, **kwargs):
    return embeddings.Embedding(semantic_vector(text), embeddings.DEFAULT_MODEL)


def fake_rerank(query, documents, **kwargs):
    query_vector = semantic_vector(query)
    def score(index):
        return sum(a * b for a, b in zip(query_vector, semantic_vector(documents[index])))
    return sorted(range(len(documents)), key=lambda index: (-score(index), index))


def run_eval(*, mode="mock", output=None, k=6):
    if mode != "mock":
        raise ValueError("Only offline mock mode is supported")
    if not 1 <= k <= 8:
        raise ValueError("k must be between 1 and 8")
    with TemporaryDirectory(prefix="memory-ab-") as directory:
        engine = create_engine(f"sqlite:///{directory}/eval.db")
        try:
            with (patch.object(db, "ENGINE", engine),
                  patch.object(db, "SessionLocal", make_session_factory(engine)),
                  patch.dict(os.environ, {"MEMORY_VECTOR_ENABLED": "1", "MEMORY_VECTOR_ROLLOUT_PERCENT": "100",
                                          "TOOL_TRACE_READ_SAMPLE": "0"}),
                  patch.object(embeddings, "embed_text", fake_embed),
                  patch.object(reranking, "rerank", fake_rerank),
                  patch.object(providers, "_post", side_effect=AssertionError("network forbidden"))):
                db.init_db()
                users = ("synthetic-A", "synthetic-B", "synthetic-C")
                expected = {}
                for uid in users:
                    with user_scope(uid):
                        expected[uid] = []
                        for index, (sentence, _) in enumerate(TOPICS):
                            text = f"{uid}: {sentence}"
                            fact = memory_facts.add_fact(text, kind="preference", confirmed=True)
                            event = episodic.write_event(uid, "chat", text)
                            expected[uid].append((fact["id"], event["id"]))
                results = []
                for uid in users:
                    old_hits = hybrid_hits = count = 0
                    with user_scope(uid):
                        for index, (_, aliases) in enumerate(TOPICS):
                            for alias in aliases:
                                query = f"What did I say about {alias}?"
                                old = episodic.retrieve_events(uid, query=query, limit=k)
                                hybrid = retrieval.retrieve(query, limit=k, token_cap=1200)
                                fact_id, event_id = expected[uid][index]
                                old_hits += any(row["id"] == event_id for row in old)
                                hybrid_hits += any(row["id"] == fact_id for row in hybrid)
                                assert all(row["text"].startswith(uid + ":") for row in hybrid)
                                count += 1
                    results.append({"user": uid, "questions": count,
                                    "old_hits": old_hits, "hybrid_hits": hybrid_hits})
        finally:
            engine.dispose()
    questions = sum(row["questions"] for row in results)
    report = {"mode": mode, "k": k, "users": len(results), "questions": questions,
              "facts_per_user": len(TOPICS),
              "old_recall_at_k": sum(row["old_hits"] for row in results) / questions,
              "hybrid_recall_at_k": sum(row["hybrid_hits"] for row in results) / questions,
              "per_user": results,
              "limitation": "Synthetic synonym oracle; not evidence of live model quality."}
    if output is not None:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["mock"], default="mock")
    parser.add_argument("--output", default=None)
    parser.add_argument("--k", type=int, default=6)
    args = parser.parse_args()
    report = run_eval(mode=args.mode, output=args.output, k=args.k)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["hybrid_recall_at_k"] >= report["old_recall_at_k"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
