"""Owner-run, transactional Postgres RLS migration (issue #60).

Run against a disposable local/CI database: python -m app.data_access.rls_migration up|down.
The caller supplies DATABASE_URL; runtime connections must use separate roles.
No passwords, role memberships, or login credentials are provisioned here.
"""
import argparse
import importlib
import os

from sqlalchemy import MetaData, Table, inspect, text

from app.data_access import public
from app.data_access.session import _PRIVATE_TABLES

APP_ROLE = "personal_agent_app"
SYSTEM_ROLE = "personal_agent_system"
POLICY = "personal_agent_owner"
SYSTEM_POLICY = "personal_agent_system_access"
# No mapped quarantine store exists yet; protect one if imported by an owner.
PRIVATE_TABLES = _PRIVATE_TABLES | {"quarantine_markers"}
# Admin operations (including business-request flag checks) use system_scope.
# These tables are neither user-owned writable stores nor public references.
ADMIN_TABLES = {"admin_user_flags", "admin_change_log", "admin_tokens", "admin_audit"}


def private_metadata():
    # Load the real models, not a parallel/simplified copy of the schema.
    for module in (
        "app.db", "app.confirm_store",
        "app.channels.binding", "app.channels.conversation", "app.channels.links",
        "app.secrets_vault", "app.agent.memory.episodic", "app.agent.proactive.store",
        "app.agent.schedules.models",
        "app.data_access.memory_facts", "app.data_access.memory_migration", "app.turn_results",
        "app.admin.models", "app.agent.harness.admin",
    ):
        importlib.import_module(module)
    from app.db import Base
    return Base.metadata


def _roles(connection):
    for name in (APP_ROLE, SYSTEM_ROLE):
        role = connection.execute(text(
            "SELECT rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolreplication "
            "FROM pg_roles WHERE rolname = :name"), {"name": name}).first()
        if role is None:
            connection.exec_driver_sql(
                f'CREATE ROLE "{name}" NOLOGIN NOSUPERUSER NOBYPASSRLS '
                'NOCREATEROLE NOCREATEDB NOREPLICATION')
        elif any(role):
            raise ValueError("RLS roles must not have administrative privileges")
    if connection.scalar(text("SELECT pg_has_role(:app, :system, 'MEMBER')"),
                         {"app": APP_ROLE, "system": SYSTEM_ROLE}):
        raise ValueError("App role must not be a member of the system role")


def _tables(connection):
    metadata = private_metadata()
    existing = set(inspect(connection).get_table_names(schema="public"))
    names = (PRIVATE_TABLES | public.PUBLIC_TABLES | ADMIN_TABLES) & existing
    reflected = MetaData()
    return [metadata.tables.get(name) if name in metadata.tables else
            Table(name, reflected, schema="public", autoload_with=connection)
            for name in sorted(names)]


def _qualified(connection, name):
    return '"public".' + connection.dialect.identifier_preparer.quote_identifier(name)


def _owner_predicate(connection, table):
    uid = "NULLIF(current_setting('app.user_id', true), '')"
    quote = connection.dialect.identifier_preparer.quote_identifier
    if "user_id" in table.c:
        return f"{quote('user_id')} = {uid}"
    parents = {
        "channel_conversations": ("channel_bindings", ("channel", "external_user_id"), "user_id"),
    }
    if table.name not in parents:
        raise ValueError(f"Private table {table.name} has no ownership policy")
    parent, keys, owner = parents[table.name]
    match = " AND ".join(f"parent.{quote(key)} = {quote(table.name)}.{quote(key)}" for key in keys)
    return (f"EXISTS (SELECT 1 FROM {_qualified(connection, parent)} AS parent "
            f"WHERE {match} AND parent.{quote(owner)} = {uid})")


def upgrade(connection):
    """Create private/admin tables, then install RLS/grants in the same transaction."""
    if connection.dialect.name != "postgresql":
        raise ValueError("RLS migration requires PostgreSQL")
    connection.exec_driver_sql('SET LOCAL search_path TO public')
    _roles(connection)
    if PRIVATE_TABLES & public.PUBLIC_TABLES:
        raise ValueError("Private tables cannot be public")
    if ADMIN_TABLES & (PRIVATE_TABLES | public.PUBLIC_TABLES):
        raise ValueError("Admin tables must remain system-only")
    metadata = private_metadata()
    metadata.create_all(connection, tables=[table for table in metadata.sorted_tables
                                           if table.name in PRIVATE_TABLES | ADMIN_TABLES])
    if public.PUBLIC_TABLES - set(inspect(connection).get_table_names(schema="public")):
        raise ValueError("Registered public tables must exist before the RLS migration")
    connection.exec_driver_sql('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
    connection.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO {APP_ROLE}, {SYSTEM_ROLE}')
    for table in _tables(connection):
        name = _qualified(connection, table.name)
        unsafe_owner = connection.scalar(text("""
            SELECT pg_has_role(:app, relowner, 'USAGE') OR pg_has_role(:system, relowner, 'USAGE')
            FROM pg_class WHERE oid = to_regclass(:table)
        """), {"app": APP_ROLE, "system": SYSTEM_ROLE, "table": name})
        if unsafe_owner:
            raise ValueError("Runtime roles must not own migrated tables")
        policies = set(connection.scalars(text(
            "SELECT policyname FROM pg_policies WHERE schemaname = 'public' AND tablename = :name"
        ), {"name": table.name}))
        if policies - {POLICY, SYSTEM_POLICY}:
            raise ValueError(f"Unexpected existing policies on {table.name}")
        connection.exec_driver_sql(f'REVOKE ALL ON TABLE {name} FROM PUBLIC, {APP_ROLE}, {SYSTEM_ROLE}')
        privileges = "SELECT, INSERT" if table.name in {"admin_change_log", "admin_audit"} else "SELECT, INSERT, UPDATE, DELETE"
        connection.exec_driver_sql(f'GRANT {privileges} ON TABLE {name} TO {SYSTEM_ROLE}')
        if table.name in PRIVATE_TABLES:
            predicate = _owner_predicate(connection, table)
            connection.exec_driver_sql(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
            connection.exec_driver_sql(f'ALTER TABLE {name} FORCE ROW LEVEL SECURITY')
            for policy in (POLICY, SYSTEM_POLICY):
                connection.exec_driver_sql(f'DROP POLICY IF EXISTS {policy} ON {name}')
            connection.exec_driver_sql(
                f'CREATE POLICY {POLICY} ON {name} TO {APP_ROLE} '
                f'USING ({predicate}) WITH CHECK ({predicate})')
            connection.exec_driver_sql(
                f'CREATE POLICY {SYSTEM_POLICY} ON {name} TO {SYSTEM_ROLE} '
                'USING (true) WITH CHECK (true)')
            connection.exec_driver_sql(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {name} TO {APP_ROLE}')
        elif table.name in public.PUBLIC_TABLES:
            connection.exec_driver_sql(f'GRANT SELECT ON TABLE {name} TO {APP_ROLE}')
        for column in table.primary_key:
            sequence = connection.scalar(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                         {"table": name, "column": column.name})
            if sequence:
                connection.exec_driver_sql(f'REVOKE ALL ON SEQUENCE {sequence} FROM PUBLIC, {APP_ROLE}, {SYSTEM_ROLE}')
                roles = f'{APP_ROLE}, {SYSTEM_ROLE}' if table.name in PRIVATE_TABLES else SYSTEM_ROLE
                connection.exec_driver_sql(f'GRANT USAGE ON SEQUENCE {sequence} TO {roles}')


def downgrade(connection):
    """Remove our policies/grants, preserving all rows, tables, roles and login setup.

    Runtime access is revoked before RLS is disabled. This never exposes private
    rows to a still-running app. Schema CREATE remains revoked from PUBLIC.
    """
    if connection.dialect.name != "postgresql":
        raise ValueError("RLS migration requires PostgreSQL")
    for table in _tables(connection):
        name = _qualified(connection, table.name)
        connection.exec_driver_sql(f'REVOKE ALL ON TABLE {name} FROM {APP_ROLE}, {SYSTEM_ROLE}')
        for column in table.primary_key:
            sequence = connection.scalar(text("SELECT pg_get_serial_sequence(:table, :column)"),
                                         {"table": name, "column": column.name})
            if sequence:
                connection.exec_driver_sql(f'REVOKE ALL ON SEQUENCE {sequence} FROM {APP_ROLE}, {SYSTEM_ROLE}')
        if table.name in PRIVATE_TABLES:
            for policy in (POLICY, SYSTEM_POLICY):
                connection.exec_driver_sql(f'DROP POLICY IF EXISTS {policy} ON {name}')
            connection.exec_driver_sql(f'ALTER TABLE {name} NO FORCE ROW LEVEL SECURITY')
            connection.exec_driver_sql(f'ALTER TABLE {name} DISABLE ROW LEVEL SECURITY')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("direction", choices=("up", "down"))
    args = parser.parse_args()
    from app.data_access.postgres import database_engine
    engine = database_engine(os.environ["DATABASE_URL"])
    try:
        with engine.begin() as connection:
            (upgrade if args.direction == "up" else downgrade)(connection)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
