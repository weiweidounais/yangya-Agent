"""Offline coverage of RLS setup and unchanged SQLite behavior."""
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.dialects import postgresql

from app.data_access import IdentityRequired, system_scope, user_scope
from app.data_access.postgres import begin_transaction, database_engine
from app.data_access.rls_migration import ADMIN_TABLES, PRIVATE_TABLES, private_metadata, upgrade, downgrade
from app.data_access.session import _PRIVATE_TABLES, make_session_factory


def test_sqlite_sessions_keep_existing_transactions_without_postgres_sql():
    engine = create_engine("sqlite://")
    statements = []
    event.listen(engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, context, executemany: statements.append(statement))
    try:
        factory = make_session_factory(engine)
        for scope in (user_scope("synthetic"), system_scope()):
            with scope, factory.begin() as session:
                assert session.scalar(select(1)) == 1
        assert statements == ["SELECT 1", "SELECT 1"]
        with engine.begin() as connection:
            for migration in (upgrade, downgrade):
                with pytest.raises(ValueError, match="requires PostgreSQL"):
                    migration(connection)
    finally:
        engine.dispose()


def test_real_private_metadata_compiles_for_postgres():
    from sqlalchemy.schema import CreateTable
    metadata = private_metadata()
    assert PRIVATE_TABLES - metadata.tables.keys() == {"quarantine_markers"}
    assert ADMIN_TABLES == {"admin_user_flags", "admin_change_log", "admin_tokens", "admin_audit"}
    assert ADMIN_TABLES <= metadata.tables.keys()
    for name in (_PRIVATE_TABLES | ADMIN_TABLES) & metadata.tables.keys():
        ddl = str(CreateTable(metadata.tables[name]).compile(dialect=postgresql.dialect()))
        assert f"CREATE TABLE {name}" in ddl
        assert "DATETIME" not in ddl


@pytest.fixture
def admin_migration(monkeypatch):
    """Model an existing installation without admin tables; no network or DB."""
    from app.data_access import public, rls_migration

    metadata = private_metadata()
    existing = set(metadata.tables) - ADMIN_TABLES
    inspector = Mock()
    inspector.get_table_names.side_effect = lambda **kwargs: sorted(existing)
    monkeypatch.setattr(rls_migration, "inspect", lambda connection: inspector)
    monkeypatch.setattr(public, "PUBLIC_TABLES", frozenset())
    create = Mock(side_effect=lambda connection, tables: existing.update(t.name for t in tables))
    monkeypatch.setattr(metadata, "create_all", create)
    connection = MagicMock()
    connection.dialect = postgresql.dialect()
    connection.execute.return_value.first.return_value = (False,) * 5
    connection.scalars.return_value = []

    def scalar(statement, params):
        if "pg_get_serial_sequence" in str(statement):
            for name in ("admin_change_log", "admin_tokens", "admin_audit"):
                if params == {"table": f'"public"."{name}"', "column": "id"}:
                    return f"public.{name}_id_seq"
            return None
        return False  # Safe owner and unrelated runtime roles.

    connection.scalar.side_effect = scalar
    return connection, create, existing


def test_owner_upgrade_provisions_admin_tables_and_reapplies_restricted_grants(admin_migration):
    connection, create, existing = admin_migration
    assert not ADMIN_TABLES & existing
    for _ in range(2):
        connection.exec_driver_sql.reset_mock()
        upgrade(connection)
        assert ADMIN_TABLES <= existing
        assert ADMIN_TABLES <= {t.name for t in create.call_args.kwargs["tables"]}
        statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
        for name, privileges in (("admin_user_flags", "SELECT, INSERT, UPDATE, DELETE"),
                                 ("admin_change_log", "SELECT, INSERT"),
                                 ("admin_tokens", "SELECT, INSERT, UPDATE, DELETE"),
                                 ("admin_audit", "SELECT, INSERT")):
            table = f'"public"."{name}"'
            revoke = f"REVOKE ALL ON TABLE {table} FROM PUBLIC, personal_agent_app, personal_agent_system"
            grant = f"GRANT {privileges} ON TABLE {table} TO personal_agent_system"
            assert revoke in statements and grant in statements
            assert statements.index(revoke) < statements.index(grant)
            assert [s for s in statements if s.startswith("GRANT ") and table in s] == [grant]
        assert "REVOKE ALL ON SEQUENCE public.admin_change_log_id_seq FROM PUBLIC, personal_agent_app, personal_agent_system" in statements
        assert "GRANT USAGE ON SEQUENCE public.admin_change_log_id_seq TO personal_agent_system" in statements
        for name in ("admin_tokens", "admin_audit"):
            sequence = f"public.{name}_id_seq"
            assert f"REVOKE ALL ON SEQUENCE {sequence} FROM PUBLIC, personal_agent_app, personal_agent_system" in statements
            grant = f"GRANT USAGE ON SEQUENCE {sequence} TO personal_agent_system"
            assert [s for s in statements if s.startswith("GRANT ") and sequence in s] == [grant]
        assert not any(s.startswith("DROP TABLE") for s in statements)


def test_owner_downgrade_revokes_admin_access_without_dropping_data(admin_migration):
    connection, _, existing = admin_migration
    upgrade(connection)
    connection.exec_driver_sql.reset_mock()
    downgrade(connection)
    statements = [call.args[0] for call in connection.exec_driver_sql.call_args_list]
    for name in ADMIN_TABLES:
        assert f'REVOKE ALL ON TABLE "public"."{name}" FROM personal_agent_app, personal_agent_system' in statements
    assert "REVOKE ALL ON SEQUENCE public.admin_change_log_id_seq FROM personal_agent_app, personal_agent_system" in statements
    for name in ("admin_tokens", "admin_audit"):
        assert f"REVOKE ALL ON SEQUENCE public.{name}_id_seq FROM personal_agent_app, personal_agent_system" in statements
    assert ADMIN_TABLES <= existing
    assert not any(s.startswith(("DROP TABLE", "DELETE", "TRUNCATE", "GRANT")) for s in statements)


@pytest.mark.parametrize("name", sorted(ADMIN_TABLES))
def test_owner_migration_refuses_public_admin_tables(admin_migration, monkeypatch, name):
    from app.data_access import public

    connection, create, _ = admin_migration
    monkeypatch.setattr(public, "PUBLIC_TABLES", frozenset({name}))
    with pytest.raises(ValueError, match="Admin tables must remain system-only"):
        upgrade(connection)
    create.assert_not_called()


def test_postgres_runtime_init_never_provisions_schema(monkeypatch):
    from app import db

    engine = Mock()
    engine.dialect.name = "postgresql"
    create = Mock()
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db.Base.metadata, "create_all", create)
    db.init_db()
    create.assert_not_called()
    assert not engine.mock_calls


@pytest.mark.parametrize("scope,expected", [(None, ""), ("A' ; SELECT 1; --", "A' ; SELECT 1; --")])
def test_transaction_identity_is_bound_and_local(monkeypatch, scope, expected):
    from contextlib import nullcontext
    connection = Mock()
    connection.dialect.name = "postgresql"
    connection._rls_identity_listener = None
    connection.execute.return_value.one.return_value = Mock(
        rolsuper=False, rolbypassrls=False, owns_tables=False, system=False, app=True)
    listen = Mock()
    monkeypatch.setattr("app.data_access.postgres.event.listen", listen)
    with user_scope(scope) if scope else nullcontext():
        begin_transaction(connection)
    setting = connection.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    assert "set_config(" in str(setting)
    assert list(setting.params.values()) == ["app.user_id", expected, True]
    if expected:
        assert expected not in str(setting)
    assert listen.call_args.args[1] == "before_cursor_execute"
    with user_scope("different"), pytest.raises(IdentityRequired):
        listen.call_args.args[2](None, None, None, None, None, None)


@pytest.mark.parametrize("unsafe", ["rolsuper", "rolbypassrls", "owns_tables", "system"])
def test_runtime_rejects_privileged_app_connections(unsafe):
    connection = Mock()
    connection.dialect.name = "postgresql"
    connection._rls_identity_listener = None
    flags = dict(rolsuper=False, rolbypassrls=False, owns_tables=False, system=False, app=True)
    flags[unsafe] = True
    connection.execute.return_value.one.return_value = Mock(**flags)
    with user_scope("A"), pytest.raises(IdentityRequired):
        begin_transaction(connection)


def test_postgres_engine_normalizes_driver_without_connecting(monkeypatch):
    factory = Mock()
    monkeypatch.setattr("app.data_access.postgres.create_engine", factory)
    database_engine("postgresql://localhost/personal_agent_rls_test")
    assert factory.call_args.args[0].drivername == "postgresql+psycopg"
    with pytest.raises(ValueError):
        database_engine("sqlite://")


@pytest.fixture
def postgres_staging_session(monkeypatch):
    """Exercise the real transaction manager without a database connection."""
    from app import db

    engine = Mock()
    engine.dialect.name = "postgresql"
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(db, "ENGINE", engine)
    monkeypatch.setattr(db, "SessionLocal", Mock(return_value=session))
    return session


@pytest.mark.parametrize("failure", ["connection", "commit"])
def test_postgres_staging_wraps_persistence_failures(postgres_staging_session, failure):
    from app import db
    from app.business_stage import BusinessCommitFailure, stage_business_writes

    session = postgres_staging_session
    error = RuntimeError("synthetic persistence failure")
    getattr(session, failure).side_effect = error
    hook = Mock()
    entered = []
    phase = "setup" if failure == "connection" else "finalization"
    with pytest.raises(BusinessCommitFailure, match=f"business staging {phase} failed") as caught:
        with stage_business_writes():
            entered.append(True)
            assert db._memory_session.get() is session
            db.after_memory_commit(hook)

    assert caught.value.__cause__ is error
    assert entered == ([] if failure == "connection" else [True])
    assert session.commit.call_count == int(failure == "commit")
    assert session.rollback.call_count == int(failure == "commit")
    session.__exit__.assert_called_once()
    hook.assert_not_called()
    assert db._memory_session.get() is None
    assert db._after_commit.get() is None


@pytest.mark.parametrize("failure", ["node", "interrupt", "runtime"])
def test_postgres_staging_preserves_body_exceptions(postgres_staging_session, failure):
    from langgraph.errors import GraphInterrupt
    from app import db
    from app.agent.node_wrap import NodeWriteFailure
    from app.business_stage import stage_business_writes

    error = {"node": NodeWriteFailure({"error": "E_NODE_FAILED"}),
             "interrupt": GraphInterrupt(()),
             "runtime": RuntimeError("synthetic body failure")}[failure]
    hook = Mock()
    with pytest.raises(type(error)) as caught:
        with stage_business_writes():
            db.after_memory_commit(hook)
            raise error

    assert caught.value is error
    postgres_staging_session.commit.assert_not_called()
    postgres_staging_session.rollback.assert_called_once()
    hook.assert_not_called()
    assert db._memory_session.get() is None
    assert db._after_commit.get() is None


@pytest.mark.parametrize("failure", ["connection", "commit"])
def test_postgres_staging_node_fails_closed(postgres_staging_session, monkeypatch, uid, failure):
    from app.agent import node_wrap

    getattr(postgres_staging_session, failure).side_effect = RuntimeError("synthetic persistence failure")
    monkeypatch.setattr(node_wrap, "_sandbox_for", lambda *args: [])
    monkeypatch.setattr(node_wrap, "route_intent", lambda state: "chat")
    monkeypatch.setattr(node_wrap, "guard_node_output", lambda result, **kwargs: (result, []))
    node = Mock(return_value={"draft": {"greeting": "must not escape"},
                              "confirm_payload": {"synthetic": True}})
    result = node_wrap.wrap_node("await_confirm", node)({"user_id": uid})

    assert node.call_count == int(failure == "commit")
    assert result["error"] == "E_NODE_FAILED"
    assert result["failed_step"] == "await_confirm"
    assert not result.get("draft")
    assert not result.get("confirm_payload")
