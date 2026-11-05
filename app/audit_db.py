"""Independent SQLite storage for audit writes during business transactions."""

import threading

from sqlalchemy import create_engine, text

from app import db

AUDIT_ENGINE = create_engine(
    f"sqlite:///{db.DATA_DIR / 'audit.db'}", connect_args={"check_same_thread": False}
)
_binds = {}
_lock = threading.Lock()
_ready = set()


def bind_audit_table(table):
    # Keep ORM consumers (including admin queries) on the same audit store.
    _binds[table] = AUDIT_ENGINE
    db.SessionLocal.configure(binds=dict(_binds))


def ensure_audit_table(table):
    """Copy legacy audit rows once, atomically, retaining totals and trace ids.

    The old database is only read, so BEGIN IMMEDIATE business writes cannot
    block audit recording. A persistent marker prevents reimport on restart.
    """
    name = table.name
    with _lock:
        if name in _ready:
            return
        with AUDIT_ENGINE.connect() as dest:
            dest.execute(text("BEGIN IMMEDIATE"))
            try:
                dest.execute(text("CREATE TABLE IF NOT EXISTS audit_migrations (name TEXT PRIMARY KEY)"))
                migrated = dest.execute(
                    text("SELECT 1 FROM audit_migrations WHERE name = :name"), {"name": name}
                ).scalar()
                if not migrated:
                    if db.ENGINE.dialect.name == "postgresql":
                        # Postgres has no legacy SQLite audit tables to copy.
                        table.create(dest, checkfirst=True)
                        dest.execute(text("INSERT INTO audit_migrations (name) VALUES (:name)"), {"name": name})
                        dest.commit()
                        _ready.add(name)
                        return
                    with db.ENGINE.connect() as source:
                        ddl = source.execute(text(
                            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"
                        ), {"name": name}).scalar()
                        if ddl:
                            dest.execute(text(ddl))
                            rows = source.execute(text(f'SELECT * FROM "{name}"'))
                            columns = ', '.join(f'"{c}"' for c in rows.keys())
                            placeholders = ', '.join('?' for _ in rows.keys())
                            while batch := rows.fetchmany(500):
                                dest.exec_driver_sql(
                                    f'INSERT INTO "{name}" ({columns}) VALUES ({placeholders})',
                                    [tuple(row) for row in batch],
                                )
                        else:
                            table.create(dest)
                    dest.execute(text("INSERT INTO audit_migrations (name) VALUES (:name)"), {"name": name})
                dest.commit()
            except Exception:
                dest.rollback()
                raise
        _ready.add(name)
