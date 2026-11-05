"""Schema setup for private tables."""


def ensure_tables(engine, *, tables=None):
    """SQLite keeps lazy schema setup; Postgres uses the explicit owner migration."""
    if engine.dialect.name == "sqlite":
        from app.db import Base
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=tables)
