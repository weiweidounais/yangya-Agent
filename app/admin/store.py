"""Changes and their hash-only journal commit or roll back together."""
from contextlib import contextmanager
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError

from app import db
from app.data_access.admin import admin_session
from app.admin.models import AdminChangeLog


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value):
    return hashlib.sha256((value if isinstance(value, str) else encoded(value)).encode()).hexdigest()


@contextmanager
def session(*, write=False):
    try:
        db.init_db()
        with admin_session(write=write) as s:
            yield s
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "admin_store_unavailable", "message": "管理存储或变更日志不可写"}) from None


def change(s, principal, entity_type, entity_id, before, after):
    s.add(AdminChangeLog(entity_type=entity_type, entity_id=str(entity_id),
                         before_hash=sha(before), after_hash=sha(after), principal=principal.name))
    s.flush()
