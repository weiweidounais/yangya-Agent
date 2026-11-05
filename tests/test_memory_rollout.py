"""Stable rollout and trusted-identity integration using synthetic owners."""
import hashlib

import pytest

from app.agent import context_assembler, tool_registry
from app.agent.memory import migration, retrieval, write_path
from app.data_access import IdentityRequired, system_scope, user_scope
from app.data_access import memory_facts
from app.feature_flags import memory_vector_enabled
from app.gateway import embeddings, reranking
from tests.test_memory_migration import database, seed


@pytest.fixture(autouse=True)
def flags(monkeypatch):
    for key in ("MEMORY_VECTOR_ENABLED", "MEMORY_VECTOR_ROLLOUT_PERCENT", "MEMORY_VECTOR_ALLOW_USERS"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("percent", [0, 10, 30, 50, 100])
def test_stable_percentage_buckets(monkeypatch, percent):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", str(percent))
    selected = []
    for index in range(1000):
        uid = f"synthetic-rollout-{index}"
        with user_scope(uid):
            enabled = memory_vector_enabled()
        expected_bucket = int.from_bytes(hashlib.sha256(uid.encode()).digest()[:8], "big") % 10000
        assert enabled == (expected_bucket < percent * 100)
        with user_scope(uid):
            assert memory_vector_enabled() == enabled
        selected.append(enabled)
    assert abs(sum(selected) - percent * 10) <= 40
    if percent in (0, 100):
        assert sum(selected) == percent * 10


def test_allowlist_kill_switch_and_defaults(monkeypatch):
    with user_scope("synthetic-A"):
        assert not memory_vector_enabled()
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
        assert not memory_vector_enabled()
        monkeypatch.setenv("MEMORY_VECTOR_ALLOW_USERS", " , synthetic-A, synthetic-C, ")
        assert memory_vector_enabled()
    with user_scope("synthetic-B"):
        assert not memory_vector_enabled()
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "0")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    with user_scope("synthetic-A"):
        assert not memory_vector_enabled()


@pytest.mark.parametrize("percent", ["-1", "101", "nonsense", "20.5", ""])
def test_invalid_percent_fails_closed(monkeypatch, percent):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", percent)
    with user_scope("synthetic-A"):
        assert not memory_vector_enabled()


def test_missing_identity_and_request_metadata_cannot_enable(monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ALLOW_USERS", "synthetic-A")
    with pytest.raises(IdentityRequired):
        memory_vector_enabled()
    with system_scope(), pytest.raises(IdentityRequired):
        memory_vector_enabled()
    assert tool_registry.get("memory_facts_search") is None
    with user_scope("synthetic-B"):
        result = tool_registry.invoke_tool("memory_facts_search", {"query": "interests", "user_id": "synthetic-A"},
                                           {"user_id": "synthetic-A"})
        assert result["error"]["code"] == "unknown_tool"


@pytest.mark.parametrize("global_flag", [None, "1"])
def test_defaults_identical_context_no_tool_or_vector_calls(database, monkeypatch, global_flag):
    seed(profile={"name": "Synthetic User", "interests": ["Python"]}, summary="Python backend")
    with user_scope("synthetic-A"):
        baseline = context_assembler.assemble_context({"user_id": "synthetic-A"}, query="Python")
        assert "Python backend" in baseline
    if global_flag is None:
        monkeypatch.delenv("MEMORY_VECTOR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", global_flag)
    def forbidden(*a, **kw):
        pytest.fail("disabled vector path touched")
    for obj, name in ((embeddings, "embed_text"), (reranking, "rerank"),
                      (memory_facts, "knn"), (memory_facts, "keyword_search"),
                      (memory_facts, "structured_search")):
        monkeypatch.setattr(obj, name, forbidden)
    with user_scope("synthetic-A"):
        assert context_assembler.assemble_context({"user_id": "synthetic-A"}, query="Python") == baseline
        assert retrieval.retrieve("Python") == []
        assert write_path.prepare_turn(None) == []
        assert tool_registry.get("memory_facts_search") is None
        assert "memory_facts_search" not in {t["name"] for t in tool_registry.list_tools()}
        assert memory_facts.add_fact("disabled") is None


def test_allowlisted_tool_and_context_use_trusted_user(database, monkeypatch):
    seed(profile={"interests": ["Python"]}, summary="")
    seed("synthetic-B", {"interests": ["Rust"]}, "")
    list(migration.migrate_users(["synthetic-A", "synthetic-B"], apply=True))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ALLOW_USERS", "synthetic-A")
    monkeypatch.setattr(embeddings, "embed_text", lambda *a, **kw: embeddings.Embedding([1.] + [0.] * 1023, "synthetic"))
    monkeypatch.setattr(reranking, "rerank", lambda q, docs, **kw: list(range(len(docs))))
    with user_scope("synthetic-A"):
        assert tool_registry.get("memory_facts_search") is not None
        result = tool_registry.invoke_tool("memory_facts_search", {"query": "Python", "user_id": "synthetic-B"})
        assert result and all("Rust" not in row["text"] for row in result)
        assert "interests: Python" in context_assembler.memory_context("synthetic-B", query="Python")
    with user_scope("synthetic-B"):
        assert tool_registry.get("memory_facts_search") is None
        assert write_path.prepare_turn(None) == []
