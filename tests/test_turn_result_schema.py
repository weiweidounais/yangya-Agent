"""Turn persistence must register before the HTTP application is imported."""

from pathlib import Path
import subprocess
import sys


def test_turn_result_registered_and_created_without_http_import(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SYSTEM_DATABASE_URL", raising=False)
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from sqlalchemy import inspect
from app import db
from app.data_access.session import _PRIVATE_TABLES, owner_column

assert "app.main" not in sys.modules
assert "app.turn_results" not in sys.modules
models = {m.class_.__tablename__: m.class_ for m in db.Base.registry.mappers}
assert models["turn_results"] is db.TurnResult
assert "turn_results" in _PRIVATE_TABLES
assert owner_column(db.TurnResult) is db.TurnResult.user_id
db.init_db()
assert "turn_results" in inspect(db.ENGINE).get_table_names()
from app import turn_results
assert turn_results.TurnResult is db.TurnResult
"""],
        cwd=Path(__file__).resolve().parents[1],
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
