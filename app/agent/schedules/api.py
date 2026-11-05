"""Authenticated panel endpoints; identity comes from the existing middleware."""
from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StrictBool
from app.data_access import current_user, NotFound
from . import service, runner

router = APIRouter(prefix='/api/schedules')


def call(fn, *args, **kwargs):
    uid = current_user()
    if not uid:
        raise HTTPException(401, '请先登录')
    try:
        return {'status': 'schedules', 'data': fn(uid, *args, **kwargs)}
    except NotFound:
        raise HTTPException(404, '没有找到这条定时任务') from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


@router.get('')
def listing(job_kind: str | None = None):
    def result(uid):
        items = service.list_schedules(uid, job_kind)
        return {'schedules': items, 'stats': {k: sum(s[k] for s in items) for k in ('run_count', 'success_count', 'fail_count')}}
    return call(result)


@router.post('')
def create(body: dict = Body(...)):
    return call(service.mutate, 'create', {k: v for k, v in body.items() if k != 'user_id'})


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool
    user_id: str = ''  # Injected and verified by SessionIdentityMiddleware.


@router.post('/proposals/{proposal_id}/decide')
def decide(proposal_id: str, body: Decision):
    return call(service.decide, proposal_id, body.confirmed)


@router.get('/{schedule_id}')
def detail(schedule_id: int):
    return call(service.get_schedule, schedule_id)


@router.get('/{schedule_id}/runs')
def history(schedule_id: int, limit: int = Query(30, ge=1, le=100)):
    return call(service.runs, schedule_id, limit)


@router.patch('/{schedule_id}')
def update(schedule_id: int, body: dict = Body(...)):
    return call(service.mutate, 'update', {'id': schedule_id, 'patch': {k: v for k, v in body.items() if k != 'user_id'}})


@router.post('/{schedule_id}/pause')
def pause(schedule_id: int):
    return call(service.mutate, 'pause', {'id': schedule_id})


@router.post('/{schedule_id}/resume')
def resume(schedule_id: int):
    return call(service.mutate, 'resume', {'id': schedule_id})


@router.delete('/{schedule_id}')
def delete(schedule_id: int):
    return call(service.mutate, 'delete', {'id': schedule_id})


@router.post('/{schedule_id}/run_now')
def run_now(schedule_id: int):
    return call(runner.run_schedule, schedule_id, manual=True)
