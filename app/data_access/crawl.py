"""Independent SQLite cache/audit ledger; atomic quotas shared by local workers.

Source data is public and globally shared. Audit rows contain trusted user IDs,
never credentials. This DB deliberately does not join profile transactions.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class CrawlStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS crawl_cache (
                    adapter TEXT, key TEXT, expires REAL, payload TEXT,
                    PRIMARY KEY(adapter, key));
                CREATE TABLE IF NOT EXISTS crawl_audit (
                    id INTEGER PRIMARY KEY, user_id TEXT, adapter TEXT, url TEXT,
                    status TEXT, bytes INTEGER, fetched_at REAL);
                CREATE TABLE IF NOT EXISTS crawl_quota (
                    scope TEXT, period TEXT, used INTEGER,
                    PRIMARY KEY(scope, period));
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, adapter, key, now):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM crawl_cache WHERE adapter=? AND key=? AND expires>?",
                             (adapter, key, now)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, adapter, key, payload, expires):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO crawl_cache VALUES (?, ?, ?, ?)",
                       (adapter, key, expires, json.dumps(payload)))

    def reserve(self, scope, period, amount, cap):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO crawl_quota VALUES (?, ?, 0)", (scope, period))
            cursor = db.execute("UPDATE crawl_quota SET used=used+? WHERE scope=? AND period=? AND used+?<=?",
                                (amount, scope, period, amount, cap))
            return cursor.rowcount == 1

    def refund(self, scope, period, amount):
        with self.connect() as db:
            db.execute("UPDATE crawl_quota SET used=MAX(0, used-?) WHERE scope=? AND period=?",
                       (amount, scope, period))

    def audit(self, user_id, adapter, url, status, size, now):
        with self.connect() as db:
            db.execute("INSERT INTO crawl_audit(user_id,adapter,url,status,bytes,fetched_at) VALUES (?,?,?,?,?,?)",
                       (user_id, adapter, url, status, size, now))
