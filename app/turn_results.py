"""Persist browser turn responses before sending them over a fallible connection."""

import json
import time

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.data_access import current_user
from app.data_access.session import memory_session
from app.db import TurnResult, init_db
from app.observability.tracing import current_trace_id


TURN_TIMEOUT = 600  # Also bounds recovery when a worker exits without a response.


def run(request_id, action):
    # Other clients retain the existing synchronous API contract.
    if not request_id:
        return action()
    uid = current_user()
    init_db()
    try:
        with memory_session() as session:
            session.add(TurnResult(user_id=uid, request_id=request_id, started_at=time.time()))
            session.flush()
    except IntegrityError:
        # Never re-execute a turn under an already accepted delivery identifier.
        raise HTTPException(409, detail={"code": "turn_exists"}) from None
    try:
        response = action()
    except Exception:
        _finish(uid, request_id, "failed")
        raise
    payload = response.model_dump()
    payload["traceId"] = current_trace_id()
    _finish(uid, request_id, "completed", payload)
    return response


def _finish(uid, request_id, status, response=None):
    with memory_session() as session:
        row = session.get(TurnResult, (uid, request_id))
        row.status = status
        row.response_json = json.dumps(response, ensure_ascii=False) if response else ""


def get(request_id):
    init_db()
    with memory_session() as session:
        row = session.get(TurnResult, (current_user(), request_id))
        if row is None:
            raise HTTPException(404, detail={"code": "turn_not_found"})
        if row.status == "completed":
            return {"status": "completed", "response": json.loads(row.response_json)}
        if row.status == "failed":
            return {"status": "failed", "code": "turn_failed"}
        if time.time() - row.started_at >= TURN_TIMEOUT:
            return {"status": "failed", "code": "turn_timeout"}
        return {"status": "running"}
