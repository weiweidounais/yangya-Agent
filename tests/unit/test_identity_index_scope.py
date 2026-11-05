"""Index backfill must not inherit authority or cache state from another turn."""
import time
import uuid

import pytest
from sqlalchemy import select

from app import db
from app.agent.harness import identity, leak
from app.data_access import IdentityRequired, current_user, user_scope


@pytest.mark.parametrize("warm", [False, True])
def test_anonymous_scan_uses_existing_index_without_private_backfill(monkeypatch, warm):
    owner = uuid.uuid4().hex
    with user_scope(owner):
        db.save_profile(owner, {"name": "王小明"})
    state = {"at": time.monotonic() if warm else 0.0,
             "engine": db.ENGINE if warm else None, "scope": owner}
    monkeypatch.setattr(identity, "_sync_state", state.copy())
    with pytest.raises(IdentityRequired):
        current_user()
    assert identity.sync_index(force=True) == 0
    result = leak.scan_output("王小明", user_id="", strict=True)
    assert result["text"] == leak.OTHER_USER_PLACEHOLDER
    assert identity._sync_state == state
    # Skipping private backfill does not grant an anonymous caller access.
    with db.SessionLocal() as session, pytest.raises(IdentityRequired):
        session.scalar(select(db.UserProfile))


def test_backfill_cache_is_scoped_to_the_trusted_user(monkeypatch):
    monkeypatch.setattr(identity, "_sync_state", {"at": 0.0, "engine": None, "scope": None})
    owners = [uuid.uuid4().hex, uuid.uuid4().hex]
    for owner in owners:
        with user_scope(owner), db.SessionLocal.begin() as session:
            # Simulate pre-index profiles without invoking save_profile's index hook.
            session.add(db.UserProfile(user_id=owner, profile_json='{"name": "王小明"}'))
    with user_scope(owners[0]):
        assert identity.sync_index() == 1
        assert identity.sync_index() == 0
        with db.SessionLocal() as session:
            assert session.get(identity.IdentityIndexed, owners[1]) is None
    with user_scope(owners[1]):
        # A recent scan for the first owner cannot suppress this owner's backfill.
        assert identity.sync_index() == 1
        assert identity.sync_index() == 0
        with db.SessionLocal() as session:
            assert session.get(identity.IdentityIndexed, owners[1]) is not None
            assert session.get(db.UserProfile, owners[0]) is None
