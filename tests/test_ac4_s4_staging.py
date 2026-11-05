"""The staging snapshot API keeps raw connections inside the DAL."""
import uuid

import pytest
from sqlalchemy.orm import Session

from app import db
from app.data_access import system_scope, user_scope
from app.data_access.session import SessionLocal
from app.data_access.staging import workspace_rows


def test_workspace_rows_checks_current_owner_on_reused_session():
    a, b = uuid.uuid4().hex, uuid.uuid4().hex
    with system_scope(), SessionLocal.begin() as session:
        session.add_all([db.UserProfile(user_id=a), db.UserProfile(user_id=b)])
    with SessionLocal() as session:
        with user_scope(a):
            assert set(workspace_rows(session, db.UserProfile.__table__)) == {(a,)}
        with user_scope(b):
            assert set(workspace_rows(session, db.UserProfile.__table__)) == {(b,)}
        assert workspace_rows(session, db.UserProfile.__table__) == {}


def test_workspace_rows_rejects_unscoped_sessions():
    with Session(db.ENGINE) as session:
        with pytest.raises(TypeError, match="require ScopedSession"):
            workspace_rows(session, db.UserProfile.__table__)
