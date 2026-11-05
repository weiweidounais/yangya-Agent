"""Synthetic legacy stores only; migration never calls a live embedding provider."""
import hashlib
import json

import pytest
from sqlalchemy import create_engine, event, inspect, select

from app import db
from app.agent.memory import migration
from app.agent.memory.episodic import EpisodicEvent
from app.data_access import IdentityRequired, NotFound, current_user, system_scope, user_scope
from app.data_access import memory_migration as store, memory_facts
from app.data_access.session import make_session_factory
from app.gateway import embeddings, providers
from scripts.migrate_memory_facts import main

MIGRATION_EMBED = embeddings.embed_for_migration


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/synthetic.db")
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", make_session_factory(engine))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "0")
    monkeypatch.delenv("MEMORY_VECTOR_ALLOW_USERS", raising=False)
    monkeypatch.delenv("MEMORY_VECTOR_ROLLOUT_PERCENT", raising=False)
    monkeypatch.setenv("PII_BACKEND", "regex")
    def forbidden(*args, **kwargs):
        pytest.fail("network forbidden")
    monkeypatch.setattr(providers, "_post", forbidden)
    calls = []
    def embed(text):
        assert db._memory_session.get() is None
        calls.append((current_user(), text))
        value = hashlib.sha256(text.encode()).digest()[0] + 1
        return embeddings.Embedding([float(value), 1.] + [0.] * 1022, "synthetic")
    monkeypatch.setattr(embeddings, "embed_for_migration", embed)
    db.init_db()
    yield engine, calls
    engine.dispose()


def seed(uid="synthetic-A", profile=None, summary="May prefer remote work. May enjoy mentoring."):
    with user_scope(uid), db.SessionLocal.begin() as session:
        session.add(db.UserProfile(user_id=uid, profile_json=json.dumps(profile or {
            "city": "Test City", "interests": ["Python", "SQL", "  Python  "],
            "facts": ["Built services; Led reviews"],
            "dialogue_summary": "May prefer small teams.",
        })))
        session.add(EpisodicEvent(user_id=uid, kind="summary", summary=summary, thread_id="synthetic-thread"))


def snapshot(uid="synthetic-A"):
    with user_scope(uid):
        return store.snapshot()


def test_dry_run_default_no_writes_embeddings_or_schema_changes(database, capsys):
    engine, calls = database
    seed()
    # Legacy DBs can predate both new tables. A preview must not create them.
    store.MigrationReceipt.__table__.drop(engine)
    memory_facts.MemoryFact.__table__.drop(engine)
    before = snapshot()
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, sql, args, ctx, many: statements.append(sql))
    assert main(["--user", "synthetic-A"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"user_hash": migration.user_hash("synthetic-A"), "would_add": 8,
                      "skip": 1, "existing": 0, "added": 0, "failed": False}
    assert calls == [] and snapshot() == before
    assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
                   for sql in statements)
    assert "memory_facts" not in inspect(engine).get_table_names()
    assert "memory_migration_receipts" not in inspect(engine).get_table_names()


def test_apply_flags_provenance_and_rerun(database):
    seed()
    assert list(migration.migrate_users(["synthetic-A"], apply=True))[0]["added"] == 8
    with user_scope("synthetic-A"), db.SessionLocal() as session:
        rows = session.scalars(select(memory_facts.MemoryFact)).all()
        receipts = session.scalars(select(store.MigrationReceipt)).all()
        assert len(rows) == len(receipts) == 8
        assert {row.text for row in rows if row.confirmed} == {
            "city: Test City", "interests: Python", "interests: SQL", "facts: Built services", "facts: Led reviews"}
        assert len([row for row in rows if not row.confirmed]) == 3
        assert all(row.source_event_id and row.thread_id == "synthetic-thread" for row in rows if row.kind == "event")
        assert {row.source for row in receipts} >= {"migration:profile:city", "migration:profile:dialogue_summary"}
        assert all(row.embed_model == "synthetic" and len(row.embedding) == 4096 for row in rows)
    before = snapshot()
    calls = len(database[1])
    result = list(migration.migrate_users(["synthetic-A"], apply=True))[0]
    assert result["added"] == result["would_add"] == 0 and result["existing"] == 8
    assert snapshot() == before and len(database[1]) == calls


@pytest.mark.parametrize("change", ["invalidate", "update", "delete"])
def test_invalidated_updated_deleted_facts_never_resurrect(database, monkeypatch, change):
    seed(profile={"city": "Test City"}, summary="")
    list(migration.migrate_users(["synthetic-A"], apply=True))
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    with user_scope("synthetic-A"):
        fact_id = store.snapshot()["facts"][0]["id"]
        if change == "delete":
            assert memory_facts.delete_fact(fact_id)
        else:
            assert memory_facts.invalidate_fact(fact_id)
            if change == "update":
                memory_facts.add_fact("city: New City", embedding=[1.] + [0.] * 1023, confirmed=True)
        before = store.snapshot()
        assert migration.migrate_user(apply=True)["added"] == 0
        assert store.snapshot() == before
        if change != "delete":
            assert memory_facts.get_fact(fact_id)["valid_until"] is not None
    assert len(database[1]) == 1


def test_a_never_reads_or_writes_b_and_receipts_are_scoped(database):
    seed()
    seed("synthetic-B", {"interests": ["B exclusive"]}, "B exclusive event")
    before = snapshot("synthetic-B")
    statements = []
    event.listen(database[0], "before_cursor_execute",
                 lambda c, cur, sql, args, ctx, many: statements.append((sql, args)))
    list(migration.migrate_users(["synthetic-A"], apply=True))
    for sql, args in statements:
        if "SELECT" in sql and any(f"FROM {table}" in sql for table in (
                "user_profiles", "episodic_events", "memory_facts", "memory_migration_receipts")):
            assert ".user_id = ?" in sql and "synthetic-A" in args
        assert "synthetic-B" not in args
    assert all(uid == "synthetic-A" and "B exclusive" not in text for uid, text in database[1])
    assert snapshot("synthetic-B") == before
    with user_scope("synthetic-B"), db.SessionLocal() as session:
        assert session.scalars(select(store.MigrationReceipt)).all() == []
        session.add(store.MigrationReceipt(user_id="synthetic-A", dedupe_key="forged", source="fake"))
        with pytest.raises(NotFound):
            session.flush()
    with pytest.raises(IdentityRequired):
        store.snapshot()
    with system_scope(), pytest.raises(IdentityRequired):
        store.snapshot()


def test_failure_isolation_private_logs_and_resume(database, monkeypatch, caplog, capsys):
    seed()
    seed("synthetic-B")
    embed = embeddings.embed_for_migration
    def fail(text):
        if current_user() == "synthetic-A" and "SQL" in text:
            raise RuntimeError("synthetic-A private fact SQL")
        return embed(text)
    monkeypatch.setattr(embeddings, "embed_for_migration", fail)
    assert main(["--apply", "--user", "synthetic-A", "--user", "synthetic-B"]) == 1
    output = capsys.readouterr().out
    reports = [json.loads(line) for line in output.splitlines()]
    assert reports[0]["failed"] and reports[1]["added"] == 8
    assert snapshot()["facts"] == [] and snapshot()["receipts"] == {}
    for private in ("synthetic-A", "synthetic-B", "SQL", "private fact"):
        assert private not in output + caplog.text
    monkeypatch.setattr(embeddings, "embed_for_migration", embed)
    reports = list(migration.migrate_users(["synthetic-A", "synthetic-B"], apply=True))
    assert reports[0]["added"] == 8 and reports[1]["existing"] == 8


def test_atomic_receipts_and_facts_rollback_on_store_error(database, monkeypatch):
    seed()
    real = store.vector_blob
    calls = []
    def fail(vector):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("synthetic failure")
        return real(vector)
    monkeypatch.setattr(store, "vector_blob", fail)
    assert list(migration.migrate_users(["synthetic-A"], apply=True))[0]["failed"]
    assert snapshot()["facts"] == [] and snapshot()["receipts"] == {}


def test_enumeration_limit_repeated_users_and_event_only_users(database):
    seed()
    seed("synthetic-B")
    with user_scope("synthetic-C"), db.SessionLocal.begin() as session:
        session.add(EpisodicEvent(user_id="synthetic-C", kind="chat", summary="May prefer testing"))
    assert len(list(migration.migrate_users(limit=2))) == 2
    reports = list(migration.migrate_users())
    assert {r["user_hash"] for r in reports} == {migration.user_hash(f"synthetic-{c}") for c in "ABC"}
    assert len(list(migration.migrate_users(["synthetic-A", "synthetic-A"]))) == 1
    assert list(migration.migrate_users(limit=0)) == []
    with user_scope("synthetic-A"), pytest.raises(IdentityRequired):
        next(store.iter_user_ids())
    assert database[1] == []


def test_redaction_stable_normalization_and_source_change_retry(database, monkeypatch):
    # Contact fields are not profile memory; they must never reach the provider.
    seed(profile={"interests": ["Ｐｙｔｈｏｎ", " python "], "phone": "PRIVATE PHONE FIELD"},
         summary="Email applicant@example.test; password: synthetic-hidden")
    list(migration.migrate_users(["synthetic-A"], apply=True))
    sent = " ".join(text for _, text in database[1])
    assert "PRIVATE PHONE FIELD" not in sent and "applicant@example.test" not in sent
    assert "synthetic-hidden" not in sent
    assert len([text for _, text in database[1] if text.startswith("interests:")]) == 1
    seed("synthetic-B", {"interests": ["Rust"]}, "")
    original = embeddings.embed_for_migration
    def concurrent_edit(text):
        with db.SessionLocal.begin() as session:
            row = session.get(db.UserProfile, "synthetic-B")
            row.profile_json = json.dumps({"memory_rev": 1})
        return original(text)
    monkeypatch.setattr(embeddings, "embed_for_migration", concurrent_edit)
    assert list(migration.migrate_users(["synthetic-B"], apply=True))[0]["failed"]
    assert snapshot("synthetic-B")["facts"] == []


def test_preexisting_tombstone_is_recorded_without_embedding(database, monkeypatch):
    seed(profile={"city": "Test City"}, summary="")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "1")
    monkeypatch.setenv("MEMORY_VECTOR_ROLLOUT_PERCENT", "100")
    with user_scope("synthetic-A"):
        old = memory_facts.add_fact("city: Test City", embedding=[1.] + [0.] * 1023)
        memory_facts.invalidate_fact(old["id"])
        report = migration.migrate_user(apply=True)
        assert report["existing"] == 1 and report["added"] == 0
        assert len(store.snapshot()["receipts"]) == 1
        assert memory_facts.get_fact(old["id"])["valid_until"] is not None
    assert database[1] == []


def test_owner_enumeration_pages_only_ids(database):
    for index in range(105):
        uid = f"synthetic-page-{index:03}"
        seed(uid, {"interests": ["Synthetic interest"]}, "")
    queries = []
    event.listen(database[0], "before_cursor_execute",
                 lambda c, cur, sql, args, ctx, many: queries.append(sql))
    with system_scope():
        owners = list(store.iter_user_ids(batch_size=10))
    assert owners == [f"synthetic-page-{index:03}" for index in range(105)]
    reads = [sql for sql in queries if "SELECT DISTINCT" in sql]
    assert len(reads) >= 22
    assert all("LIMIT" in sql and "user_id >" in sql for sql in reads)
    assert not any("profile_json" in sql or "summary" in sql for sql in reads)


def test_migration_embedding_client_requires_scope_and_uses_existing_gateway(database, monkeypatch):
    from types import SimpleNamespace

    with pytest.raises(IdentityRequired):
        MIGRATION_EMBED("synthetic fact")
    with system_scope(), pytest.raises(IdentityRequired):
        MIGRATION_EMBED("synthetic fact")
    spec = SimpleNamespace(api_key=lambda: "synthetic", base_url="https://example.invalid/v1",
                           params={}, model="synthetic", timeout=1)
    monkeypatch.setattr(embeddings, "get_config", lambda: SimpleNamespace(embedding_model=lambda task: spec))
    calls = []
    def post(*args):
        calls.append(args)
        return {"data": [{"embedding": [1.] + [0.] * 1023}]}
    monkeypatch.setattr(providers, "_post", post)
    with user_scope("synthetic-A"):
        assert embeddings.embed_text("disabled runtime") is None
        result = MIGRATION_EMBED("synthetic fact")
    assert len(calls) == 1 and calls[0][3]["input"] == "synthetic fact"
    assert result.model == "synthetic" and len(result.vector) == 1024
