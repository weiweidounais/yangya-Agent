"""AC1–AC6: real Postgres, raw SQL, synthetic rows, disposable CI database only."""
import importlib.util
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import (Boolean, Column, DateTime, Float, Integer, LargeBinary,
                        MetaData, String, Table, Text, create_engine, func, select, text)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.sql.ddl import sort_tables

from app.data_access import IdentityRequired, system_scope, user_scope
from app.data_access import public
from app.data_access.rls_migration import PRIVATE_TABLES, downgrade, private_metadata, upgrade
from app.data_access.session import make_session_factory


@pytest.fixture(scope="module")
def pg():
    url = os.getenv("RLS_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    required = os.getenv("RLS_TEST_REQUIRED") == "1"
    if not url or importlib.util.find_spec("psycopg") is None:
        if required:
            pytest.fail("CI requires a Postgres URL and psycopg")
        pytest.skip("RLS tests require a disposable local/CI Postgres and psycopg")
    parsed = make_url(url)
    # Do not connect to arbitrary configured databases, even accidentally.
    if (parsed.get_backend_name() != "postgresql"
            or parsed.host not in {"localhost", "127.0.0.1", "::1", "postgres"}
            or parsed.port in {8788, 8789}
            or parsed.database != "personal_agent_rls_test"
            or parsed.query):
        pytest.fail("RLS tests require the disposable local/CI personal_agent_rls_test database")
    parsed = parsed.set(drivername="postgresql+psycopg")
    owner = create_engine(parsed, connect_args={"connect_timeout": 3})
    try:
        with owner.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        owner.dispose()
        if required:
            pytest.fail("CI Postgres service is unavailable", pytrace=False)
        pytest.skip("Disposable local/CI Postgres is unavailable")
    metadata = private_metadata()
    synthetic = MetaData()
    # There is no quarantine-marker model on main. Exercise the private
    # classification for existing/imported markers without adding a fake store.
    quarantine = Table("quarantine_markers", synthetic,
                       Column("id", Integer, primary_key=True),
                       Column("user_id", String(128), nullable=False), Column("reason", Text))
    shared = [Table(name, synthetic, Column("id", Integer, primary_key=True), Column("name", Text))
              for name in ("rls_test_companies", "rls_test_jobs", "rls_test_industry", "rls_test_templates")]
    original_public = public.PUBLIC_TABLES
    with system_scope():
        for table in shared:
            public.register_public_table(table)
    try:
        with owner.begin() as connection:
            synthetic.create_all(connection)
            upgrade(connection)
            for role, group in (("rls_test_app", "personal_agent_app"), ("rls_test_system", "personal_agent_system")):
                if not connection.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}):
                    connection.exec_driver_sql(
                        f'CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE')
                connection.exec_driver_sql(f'GRANT {group} TO {role}')
        app = create_engine(parsed.set(username="rls_test_app", password=None), pool_size=1, max_overflow=0)
        system = create_engine(parsed.set(username="rls_test_system", password=None), pool_size=1, max_overflow=0)
        tables = {name: metadata.tables[name] for name in PRIVATE_TABLES if name in metadata.tables}
        tables[quarantine.name] = quarantine
        assert tables.keys() == PRIVATE_TABLES
        yield owner, app, system, tables, shared
    finally:
        public.PUBLIC_TABLES = original_public
        for engine in (locals().get("app"), locals().get("system"), owner):
            if engine is not None:
                engine.dispose()


def row_for(table, uid, number):
    row = {}
    for column in table.c:
        if column.name == "user_id":
            value = uid
        elif isinstance(column.type, Boolean):
            value = False
        elif isinstance(column.type, Integer):
            value = number
        elif isinstance(column.type, Float):
            value = 1.0
        elif isinstance(column.type, DateTime):
            value = datetime(2026, 1, 1, tzinfo=timezone.utc)
        elif isinstance(column.type, LargeBinary):
            value = bytes(4096)
        elif column.name.endswith("_json"):
            value = "{}"
        else:
            value = f"{uid}-{column.name}"
        row[column.name] = value
    return row


@pytest.fixture
def seeded(pg):
    owner, app, system, tables, shared = pg
    # Aliases and thread links reference applications: parents first, children last.
    ordered = sort_tables(tables.values())
    with owner.begin() as connection:
        for table in [*reversed(ordered), *shared]:
            connection.execute(table.delete())
        for table in ordered:
            connection.execute(table.insert(), [row_for(table, "A", 101), row_for(table, "B", 202)])
        for table in shared:
            connection.execute(table.insert().values(id=1, name="synthetic reference"))
    yield pg


def set_user(connection, uid):
    connection.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": uid})


def raw_where(table, uid="B", number=202):
    row = row_for(table, uid, number)
    return " AND ".join(f'"{column.name}" = :{column.name}' for column in table.primary_key), row


@pytest.mark.parametrize("name", sorted(PRIVATE_TABLES))
def test_raw_select_update_delete_cannot_touch_other_owner(seeded, name):
    _, app, _, tables, _ = seeded
    table = tables[name]
    where, params = raw_where(table)
    column = next(column.name for column in table.c if not column.primary_key)
    with app.connect() as connection:
        set_user(connection, "A")
        assert connection.execute(text(f'SELECT * FROM "{name}" WHERE {where}'), params).all() == []
        assert connection.execute(text(f'UPDATE "{name}" SET "{column}" = "{column}" WHERE {where}'), params).rowcount == 0
        assert connection.execute(text(f'DELETE FROM "{name}" WHERE {where}'), params).rowcount == 0
        assert len(connection.execute(text(f'SELECT * FROM "{name}"')).all()) == 1
        assert connection.execute(text(f'UPDATE "{name}" SET "{column}" = "{column}"')).rowcount == 1
        assert connection.execute(text(f'DELETE FROM "{name}"')).rowcount == 1
        connection.rollback()


@pytest.mark.parametrize("name", sorted(PRIVATE_TABLES))
@pytest.mark.parametrize("identity", [None, ""])
def test_missing_or_empty_identity_fails_closed(seeded, name, identity):
    _, app, _, tables, _ = seeded
    column = next(column.name for column in tables[name].c if not column.primary_key)
    with app.connect() as connection:
        if identity is not None:
            set_user(connection, identity)
        assert connection.execute(text(f'SELECT * FROM "{name}"')).all() == []
        assert connection.execute(text(f'UPDATE "{name}" SET "{column}" = "{column}"')).rowcount == 0
        assert connection.execute(text(f'DELETE FROM "{name}"')).rowcount == 0


@pytest.mark.parametrize("name", sorted(PRIVATE_TABLES))
def test_insert_and_owner_transfer_are_checked(seeded, name):
    _, app, _, tables, _ = seeded
    table = tables[name]
    with app.connect() as connection:
        set_user(connection, "A")
        with pytest.raises(DBAPIError) as error:
            connection.execute(table.insert(), row_for(table, "C", 303))
        assert error.value.orig.sqlstate == "42501"
        connection.rollback()
        set_user(connection, "A")
        column = "user_id" if "user_id" in table.c else "external_user_id"
        value = "B" if column == "user_id" else f"B-{column}"
        with pytest.raises(DBAPIError) as error:
            connection.execute(text(f'UPDATE "{name}" SET "{column}" = :value'), {"value": value})
        assert error.value.orig.sqlstate == "42501"


def test_all_private_tables_force_rls_and_runtime_roles_cannot_bypass(seeded):
    owner, app, system, tables, _ = seeded
    with owner.connect() as connection:
        secured = connection.execute(text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r'
        """)).all()
        assert {name for name, rls, force in secured if rls and force} == tables.keys()
    for engine in (app, system):
        with engine.connect() as connection:
            assert connection.execute(text(
                "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
            )).one() == (False, False, False, False)
            assert not connection.scalar(text("""
                SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                               WHERE n.nspname = 'public' AND c.relkind = 'r'
                                 AND pg_has_role(current_user, c.relowner, 'USAGE'))
            """))
    with app.connect() as connection:
        with pytest.raises(DBAPIError) as error:
            connection.exec_driver_sql("SET ROLE personal_agent_system")
        assert error.value.orig.sqlstate == "42501"
        connection.rollback()
        with pytest.raises(DBAPIError):
            connection.exec_driver_sql("TRUNCATE user_profiles")


@pytest.mark.parametrize("operation", ["INSERT INTO {name} (id, name) VALUES (2, 'synthetic')",
                                      "UPDATE {name} SET name = 'synthetic'", "DELETE FROM {name}",
                                      "TRUNCATE {name}"])
def test_public_tables_readable_but_only_system_can_write(seeded, operation):
    _, app, system, _, shared = seeded
    for table in shared:
        with app.connect() as connection:
            assert connection.execute(text(f'SELECT name FROM "{table.name}"')).scalar_one() == "synthetic reference"
            with pytest.raises(DBAPIError) as error:
                connection.execute(text(operation.format(name=table.name)))
            assert error.value.orig.sqlstate == "42501"
        if not operation.startswith("TRUNCATE"):
            with system.connect() as connection:
                assert connection.execute(text(operation.format(name=table.name))).rowcount == 1


def test_scoped_sessions_set_local_on_every_transaction_and_reset_pool(seeded):
    _, app, system, _, _ = seeded
    factory = make_session_factory(app, system_engine=system)
    # Public raw-SQL guard is separate; use the connection to prove DB enforcement.
    for uid in ("A", "B", "A' ; SELECT 1; --"):
        with user_scope(uid), factory() as session:
            for finish in (session.commit, session.rollback):
                connection = session.connection()
                assert connection.scalar(select(func.current_setting("app.user_id"))) == uid
                finish()
        with app.connect() as connection:
            assert connection.scalar(text("SELECT NULLIF(current_setting('app.user_id', true), '')")) is None
            assert connection.execute(text("SELECT * FROM user_profiles")).all() == []


def test_system_scope_uses_separate_role_and_user_transactions_reject_scope_switch(seeded):
    _, app, system, _, _ = seeded
    from app.db import UserProfile
    from sqlalchemy import select
    factory = make_session_factory(app, system_engine=system)
    with system_scope(), factory() as session:
        assert {row.user_id for row in session.scalars(select(UserProfile))} == {"A", "B"}
    with user_scope("A"), factory() as session:
        assert session.get(UserProfile, "A") is not None
        assert session.get(UserProfile, "B") is None
        with user_scope("B"), pytest.raises(IdentityRequired):
            session.connection().execute(select(UserProfile))
    with system_scope(), make_session_factory(app)() as session, pytest.raises(IdentityRequired):
        session.connection()
    with user_scope("A"), make_session_factory(system)() as session, pytest.raises(IdentityRequired):
        session.connection()


def test_no_identity_and_savepoint_rollback_do_not_leak_identity(seeded):
    _, app, _, tables, _ = seeded
    factory = make_session_factory(app)
    with factory() as session:
        assert session.connection().execute(tables["user_profiles"].select()).all() == []
        session.rollback()
        with user_scope("A"):
            assert session.connection().scalar(select(func.current_setting("app.user_id"))) == "A"
            savepoint = session.begin_nested()
            assert session.connection().scalar(select(func.current_setting("app.user_id"))) == "A"
            savepoint.rollback()
            assert session.connection().scalar(select(func.current_setting("app.user_id"))) == "A"
            session.commit()
        with user_scope("B"):
            assert session.connection().execute(tables["user_profiles"].select()).mappings().one()["user_id"] == "B"


def test_postgres_memory_and_guard_transactions_commit_or_roll_back(seeded, monkeypatch):
    owner, app, system, _, _ = seeded
    from app import db
    from app.audit_db import _binds
    from app.business_stage import stage_business_writes
    factory = make_session_factory(app, system_engine=system)
    factory.configure(binds=dict(_binds))
    monkeypatch.setattr(db, "ENGINE", app)
    monkeypatch.setattr(db, "SessionLocal", factory)
    monkeypatch.setattr("app.agent.harness.identity.index_profile", lambda *args, **kwargs: None)
    with user_scope("A"):
        db.save_profile("A", {"city": "original"})
        with pytest.raises(ValueError, match="synthetic guard failure"):
            with stage_business_writes():
                db.save_profile("A", {"city": "rejected"})
                assert db.get_profile("A")["city"] == "rejected"
                raise ValueError("synthetic guard failure")
        assert db.get_profile("A")["city"] == "original"
        with stage_business_writes():
            db.save_profile("A", {"city": "accepted"})
        assert db.get_profile("A")["city"] == "accepted"
    with user_scope("B"):
        assert db.get_profile("B") == {}
    with owner.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM user_profiles")) == 2


def test_scoped_public_writes_use_system_connection(seeded):
    _, app, system, _, shared = seeded
    from app.data_access.public import PublicWriteForbidden
    factory = make_session_factory(app, system_engine=system)
    with user_scope("A"), factory() as session:
        assert len(session.execute(shared[0].select()).all()) == 1
        with pytest.raises(PublicWriteForbidden):
            session.execute(shared[0].insert().values(id=2, name="synthetic"))
    with system_scope(), factory() as session:
        session.execute(shared[0].insert().values(id=2, name="synthetic"))
        assert len(session.execute(shared[0].select()).all()) == 2


def test_downgrade_preserves_data_revokes_access_and_can_upgrade_again(seeded):
    owner, app, _, tables, _ = seeded
    with owner.begin() as connection:
        downgrade(connection)
        assert not connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_policies WHERE policyname LIKE 'personal_agent_%')"))
        for table in tables.values():
            assert len(connection.execute(table.select()).all()) == 2
            assert not connection.scalar(text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:name)"), {"name": table.name})
    try:
        with app.connect() as connection, pytest.raises(DBAPIError):
            connection.execute(text("SELECT * FROM user_profiles"))
    finally:
        with owner.begin() as connection:
            upgrade(connection)
            upgrade(connection)  # Idempotent and all grants/policies restored.
    with app.connect() as connection:
        set_user(connection, "A")
        assert connection.execute(text("SELECT user_id FROM user_profiles")).scalars().all() == ["A"]
