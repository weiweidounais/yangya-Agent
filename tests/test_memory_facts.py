"""Offline stage-A contracts, including real sqlite-vec and forced fallback."""
import hashlib
import json
import sqlite3
import struct

import httpx
import pytest
from sqlalchemy import create_engine, event, inspect, select

from app import db
from app.data_access import IdentityRequired, NotFound, system_scope, user_scope
from app.data_access import memory_facts as facts
from app.data_access.session import make_session_factory
from app.gateway import embeddings, providers
from app.gateway.config import ConfigError, get_config


def vector(x=1.0, y=0.0):
    return [x, y] + [0.0] * 1022


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def __call__(self, text):
        self.calls.append(text)
        value = hashlib.sha256(text.encode()).digest()[0] + 1
        return embeddings.Embedding(vector(value, 1.0), embeddings.DEFAULT_MODEL)


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", make_session_factory(engine))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings, "embed_text", fake)
    db.init_db()
    yield engine, fake
    engine.dispose()


@pytest.fixture(params=["sqlite-vec", "python"])
def backend(store, request, monkeypatch):
    if request.param == "python":
        def missing(connection):
            raise sqlite3.OperationalError("extension loading unavailable")
        monkeypatch.setattr(facts, "_load_sqlite_vec", missing)
    return request.param


def test_schema_and_fake_embedding(store):
    engine, fake = store
    columns = {c["name"]: c for c in inspect(engine).get_columns("memory_facts")}
    assert set(columns) == {
        "id", "user_id", "kind", "text", "embedding", "source_event_id", "thread_id",
        "created_at", "updated_at", "valid_until", "confidence", "confirmed", "embed_model",
    }
    assert columns["user_id"]["nullable"] is False
    assert any(i["column_names"] == ["user_id"] for i in inspect(engine).get_indexes("memory_facts"))
    with user_scope("A"):
        row = facts.add_fact("prefers Python", kind="skill", user_id="B", source_event_id=42,
                             thread_id="thread-a", confirmed=True, confidence=0.9)
        assert row == facts.get_fact(row["id"], user_id="B")
    assert fake.calls == ["prefers Python"]
    assert row["user_id"] == "A"
    assert row["source_event_id"] == 42 and row["thread_id"] == "thread-a"
    assert row["confirmed"] is True and row["confidence"] == 0.9
    assert row["kind"] == "skill" and row["text"] == "prefers Python"
    assert row["created_at"] and row["updated_at"] and row["valid_until"] is None
    assert row["embed_model"] == "BAAI/bge-m3"
    assert len(row["embedding"]) == 4096
    assert len(struct.unpack("<1024f", row["embedding"])) == 1024


def test_isolation_invalidation_and_physical_deletion(store, backend):
    with user_scope("B"):
        # A foreign perfect match must not displace A's own less similar match.
        foreign = facts.add_fact("B private", embedding=vector(), user_id="A")
    with user_scope("A"):
        own = facts.add_fact("A first", embedding=vector(0.8, 0.6), user_id="B")
        old = facts.add_fact("A outdated", embedding=vector())
        assert facts.invalidate_fact(old["id"], user_id="B") is True
        invalidated = facts.get_fact(old["id"])
        assert invalidated["valid_until"] is not None
        assert invalidated["updated_at"] >= invalidated["created_at"]
        assert facts.get_fact(foreign["id"], user_id="B") is None
        assert facts.invalidate_fact(foreign["id"], user_id="B") is False
        assert facts.delete_fact(foreign["id"], user_id="B") is False
        results = facts.knn(vector(), 1, user_id="B")
        assert [r["id"] for r in results] == [own["id"]]
        assert results[0]["distance"] == pytest.approx(0.2, abs=1e-6)
        assert facts.delete_fact(own["id"], user_id="B") is True
        assert facts.get_fact(own["id"]) is None
        assert facts.knn(vector(), 10) == []
        assert facts.forget_all(user_id="B") == 1  # also removes invalidated vectors
        assert facts.forget_all() == 0
        with db.SessionLocal() as session:
            assert session.scalars(select(facts.MemoryFact)).all() == []
    with user_scope("B"):
        assert facts.get_fact(foreign["id"])["valid_until"] is None
        assert [r["id"] for r in facts.knn(vector(), 10)] == [foreign["id"]]
        assert facts.forget_all(user_id="A") == 1
    with store[0].connect() as connection:
        # Embeddings live in these rows: no orphan vectors.
        assert connection.execute(select(facts.MemoryFact.__table__)).all() == []


def test_native_search_and_fallback_read_only_scoped_rows(store, backend):
    engine, _ = store
    statements = []

    @event.listens_for(engine, "before_cursor_execute")
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, parameters))

    with user_scope("A"):
        first = facts.add_fact("first", embedding=vector())
        second = facts.add_fact("second", embedding=vector(0, 1))
        third = facts.add_fact("third", embedding=vector(-1, 0))
        statements.clear()
        assert [r["id"] for r in facts.knn(vector(), 50)] == [first["id"], second["id"], third["id"]]
        reads = [(sql, args) for sql, args in statements if "FROM memory_facts" in sql]
        assert reads
        for sql, args in reads:
            assert "memory_facts.user_id = ?" in sql
            assert "memory_facts.valid_until IS NULL" in sql
            assert "A" in args
        assert any("vec_distance_cosine" in sql for sql, _ in reads) == (backend == "sqlite-vec")
        assert facts.knn(vector(), 0) == []
        assert facts.knn(vector(), -1) == []


def test_extension_failure_logs_once_across_connections(store, monkeypatch, caplog):
    calls = []

    def missing(connection):
        calls.append(connection)
        raise ImportError("sqlite_vec unavailable")

    monkeypatch.setattr(facts, "_load_sqlite_vec", missing)
    monkeypatch.setattr(facts, "_fallback_logged", False)
    with user_scope("A"):
        facts.add_fact("one", embedding=vector())
        assert len(facts.knn(vector(), 5)) == 1
        assert len(facts.knn(vector(), 5)) == 1
        store[0].dispose()
        assert len(facts.knn(vector(), 5)) == 1
    assert len(calls) == 2
    assert caplog.text.count("memory search uses user-scoped Python cosine") == 1


@pytest.mark.parametrize("flag", [None, "0", "false"])
def test_flag_off_never_touches_store_or_embedder(store, monkeypatch, flag):
    engine, fake = store
    with user_scope("A"):
        existing = facts.add_fact("keep", embedding=vector())
    if flag is None:
        monkeypatch.delenv("MEMORY_VECTOR_ENABLED")
    else:
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", flag)
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    assert facts.add_fact("disabled") is None
    assert facts.get_fact(existing["id"]) is None
    assert facts.invalidate_fact(existing["id"]) is False
    assert facts.delete_fact(existing["id"]) is False
    assert facts.forget_all() == 0
    assert facts.knn(vector(), 1) == []
    assert fake.calls == [] and statements == []
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    with user_scope("A"):
        assert facts.get_fact(existing["id"])["valid_until"] is None


def test_scope_required_and_orm_guard(store):
    operations = [lambda: facts.add_fact("x"), lambda: facts.get_fact(1),
                  lambda: facts.invalidate_fact(1), lambda: facts.delete_fact(1),
                  facts.forget_all, lambda: facts.knn(vector(), 1)]
    for operation in operations:
        with pytest.raises(IdentityRequired):
            operation()
        with system_scope(), pytest.raises(IdentityRequired):
            operation()
    assert store[1].calls == []
    with user_scope("B"):
        foreign = facts.add_fact("B", embedding=vector())
    with user_scope("A"), db.SessionLocal() as session:
        assert session.get(facts.MemoryFact, foreign["id"]) is None
        assert session.scalars(select(facts.MemoryFact)).all() == []
        session.add(facts.MemoryFact(user_id="B", text="forged", embedding=embeddings.vector_blob(vector()),
                                     embed_model=embeddings.DEFAULT_MODEL))
        with pytest.raises(NotFound):
            session.flush()


def test_operations_join_memory_transaction(store, backend):
    with user_scope("A"):
        original = facts.add_fact("original", embedding=vector())
        with pytest.raises(RuntimeError, match="rollback"), db.memory_transaction():
            assert facts.delete_fact(original["id"])
            added = facts.add_fact("rolled back", embedding=vector())
            assert [r["id"] for r in facts.knn(vector(), 5)] == [added["id"]]
            raise RuntimeError("rollback")
        assert [r["id"] for r in facts.knn(vector(), 5)] == [original["id"]]


def test_disabled_staging_does_not_read_memory_facts(store, monkeypatch):
    from app.business_stage import stage_business_writes

    engine, _ = store
    statements = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    # Workspace connections use a separate engine, so capture those too.
    from sqlalchemy.engine import Engine
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with user_scope("A"):
        existing = facts.add_fact("keep", embedding=vector())
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "0")
        statements.clear()
        event.listen(Engine, "before_cursor_execute", capture)
        try:
            with stage_business_writes():
                assert facts.forget_all() == 0
        finally:
            event.remove(Engine, "before_cursor_execute", capture)
        assert not any("memory_facts" in sql for sql in statements)
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
        assert facts.get_fact(existing["id"]) is not None


def test_enabled_staging_keeps_vectors_and_rows_atomic(store, backend):
    from app.business_stage import stage_business_writes

    with user_scope("B"):
        foreign = facts.add_fact("foreign", embedding=vector())
    with user_scope("A"):
        with stage_business_writes():
            own = facts.add_fact("staged", embedding=vector())
            assert [r["id"] for r in facts.knn(vector(), 5)] == [own["id"]]
        assert facts.get_fact(own["id"]) is not None
        with pytest.raises(RuntimeError, match="rollback"), stage_business_writes():
            assert facts.forget_all() == 1
            raise RuntimeError("rollback")
        assert [r["id"] for r in facts.knn(vector(), 5)] == [own["id"]]
    with user_scope("B"):
        assert facts.get_fact(foreign["id"]) is not None


@pytest.mark.parametrize("bad", [[1.0], [0.0] * 1024, vector(float("nan")), vector(float("inf")), vector(1e100)])
def test_invalid_vectors_are_rejected(store, bad):
    with user_scope("A"):
        with pytest.raises(ValueError):
            facts.add_fact("bad", embedding=bad)
        with pytest.raises(ValueError):
            facts.knn(bad, 1)
        assert facts.forget_all() == 0


@pytest.fixture
def embedding_transport(monkeypatch, uid):
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.delenv("LLM_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LLM_CONFIG_JSON", raising=False)
    requests = []
    response = {"data": [{"embedding": vector()}]}

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=response)

    providers.set_transport_factory(lambda: httpx.MockTransport(handle))
    yield requests, response
    providers.set_transport_factory(None)


def test_embedding_default_and_configured_task(embedding_transport, monkeypatch):
    requests, _ = embedding_transport
    # Dummy test values only; model specs use the normal gateway key-env lookup.
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-placeholder")
    result = embeddings.embed_text("prefers remote")
    assert result.model == "BAAI/bge-m3" and result.vector == vector()
    assert str(requests[0].url) == "https://api.siliconflow.cn/v1/embeddings"
    assert requests[0].headers["Authorization"] == "Bearer test-placeholder"
    assert json.loads(requests[0].content) == {
        "input": "prefers remote", "model": "BAAI/bge-m3", "encoding_format": "float",
    }
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({
        "providers": {"sf": {"base_url": "https://embedding.example.test/v1", "api_key_env": "MEMORY_TEST_API_KEY"}},
        "models": {"bge": {"provider": "sf", "model": "BAAI/bge-m3", "timeout": 7}},
        "embedding_tasks": {"memory_embed": "bge"},
    }))
    monkeypatch.setenv("MEMORY_TEST_API_KEY", "custom-test-placeholder")
    assert embeddings.embed_text("second").model == "BAAI/bge-m3"
    assert str(requests[1].url) == "https://embedding.example.test/v1/embeddings"
    assert requests[1].headers["Authorization"] == "Bearer custom-test-placeholder"
    assert requests[1].extensions["timeout"]["read"] == 7


def test_embedding_flag_off_and_missing_key(embedding_transport, monkeypatch):
    requests, _ = embedding_transport
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.delenv("MEMORY_VECTOR_ENABLED")
    assert embeddings.embed_text("off") is None
    assert requests == []
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    with pytest.raises(providers.ProviderError, match="no_key"):
        embeddings.embed_text("missing")
    assert requests == []


@pytest.mark.parametrize("response", [{}, {"data": []}, {"data": [{"embedding": [1.0]}]}])
def test_embedding_rejects_invalid_responses(embedding_transport, monkeypatch, response):
    _, payload = embedding_transport
    payload.clear()
    payload.update(response)
    monkeypatch.setenv("SILICONFLOW_API_KEY", "test-placeholder")
    with pytest.raises(providers.ProviderError, match="invalid memory embedding response"):
        embeddings.embed_text("invalid")


def test_embedding_config_rejects_unknown_model(monkeypatch):
    monkeypatch.delenv("LLM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("LLM_CONFIG_JSON", json.dumps({"embedding_tasks": {"memory_embed": "missing"}}))
    with pytest.raises(ConfigError, match="undeclared model"):
        get_config()
