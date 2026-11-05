"""Postgres connection setup; identities come exclusively from trusted scopes."""
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url

from app.data_access import IdentityRequired, current_user


def database_engine(url):
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use PostgreSQL; omit it for SQLite")
    return create_engine(parsed, pool_pre_ping=True)


def trusted_identity():
    try:
        return current_user()
    except IdentityRequired:
        return ""  # Missing identity must never inherit a pooled connection's user.


def begin_transaction(connection):
    if connection.dialect.name != "postgresql":
        return
    old_listener = getattr(connection, "_rls_identity_listener", None)
    if old_listener is not None:
        event.remove(connection, "before_cursor_execute", old_listener)
    uid = trusted_identity()
    role = connection.execute(text("""
        SELECT r.rolsuper, r.rolbypassrls,
               pg_has_role(current_user, 'personal_agent_system', 'MEMBER') AS system,
               pg_has_role(current_user, 'personal_agent_app', 'MEMBER') AS app,
               EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                         AND pg_has_role(current_user, c.relowner, 'USAGE')) AS owns_tables
        FROM pg_roles r WHERE r.rolname = current_user
    """)).one()
    if role.rolsuper or role.rolbypassrls or role.owns_tables:
        raise IdentityRequired("Postgres runtime role must not own tables or bypass RLS")
    if (uid is None and not role.system) or (uid is not None and (role.system or not role.app)):
        raise IdentityRequired("Postgres role does not match the trusted scope")
    # set_config(..., true) is the parameterized equivalent of SET LOCAL.
    # No interpolation of identities and no session-level setting survives pooling.
    connection.execute(select(func.set_config("app.user_id", uid or "", True)))

    def check_identity(conn, cursor, statement, parameters, context, executemany):
        if trusted_identity() != uid:
            raise IdentityRequired("Identity cannot change inside a Postgres transaction")

    # Connection-local, never stored on the pooled DBAPI connection.
    event.listen(connection, "before_cursor_execute", check_identity)
    connection._rls_identity_listener = check_identity
