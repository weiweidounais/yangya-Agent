"""Users endpoints; every operation uses the existing harness dependencies."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.agent.harness.admin import ALL_SCOPES, AdminPrincipal, AuditedAdminRoute, require_scope
from app.admin import users
from app.admin.models import AdminChangeLog
from app.admin.store import session

router = APIRouter(prefix="/api/admin", tags=["admin"], route_class=AuditedAdminRoute)


@router.get("/me")
def me(principal: AdminPrincipal = Depends(require_scope(""))):
    return {"principal": principal.name, "scopes": sorted(principal.scopes), "available_scopes": ALL_SCOPES}


@router.get("/users", dependencies=[Depends(require_scope("users:read"))])
def user_list(q: str = Query("", max_length=128), disabled: bool | None = None,
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    return users.list_users(q, disabled, limit, offset)


@router.get("/schedules", dependencies=[Depends(require_scope("users:read"))])
def schedules(user_id: str | None = None, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    from app.agent.schedules.models import Schedule
    from app.agent.schedules.service import serialize
    with session() as s:
        query = select(Schedule).where(Schedule.status != 'deleted')
        if user_id is not None:
            query = query.where(Schedule.user_id == user_id)
        # Operations metadata only: no prompts, names or private output bodies.
        fields = {'id', 'user_id', 'job_kind', 'enabled', 'status', 'paused_by_idle',
                  'next_run_at', 'last_run_at', 'last_status', 'run_count', 'success_count', 'fail_count'}
        return {'items': [{k: v for k, v in serialize(row).items() if k in fields}
                          for row in s.scalars(query.order_by(Schedule.id).offset(offset).limit(limit))]}


@router.get("/users/{user_id}", dependencies=[Depends(require_scope("users:read"))])
def user_detail(user_id: str, reveal: bool = False):
    return users.detail(user_id, reveal)


@router.post("/users/{user_id}/disable")
def disable(user_id: str, principal: AdminPrincipal = Depends(require_scope("users:write"))):
    return users.set_disabled(user_id, True, principal)


@router.post("/users/{user_id}/enable")
def enable(user_id: str, principal: AdminPrincipal = Depends(require_scope("users:write"))):
    return users.set_disabled(user_id, False, principal)


@router.get("/changes", dependencies=[Depends(require_scope("audit:read"))])
def changes(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with session() as s:
        return {"items": [{c.name: getattr(row, c.name) for c in AdminChangeLog.__table__.columns}
                          for row in s.scalars(select(AdminChangeLog).order_by(
                              AdminChangeLog.id.desc()).offset(offset).limit(limit))]}
