"""Chat tools only propose create/update/delete; models cannot approve them."""
from functools import wraps
from app.agent.tool_registry import ToolError, tool
from . import service, runner


def validation_errors(fn):
    """Known validation refusals must not become unknown write outcomes."""
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            raise ToolError('schedule_validation', str(exc)) from exc
    return wrapped


@tool('schedule.create', risk_level='self_write')
@validation_errors
def create(user_id: str, schedule: dict) -> dict:
    return service.propose(user_id, 'create', schedule)


@tool('schedule.list', risk_level='read')
@validation_errors
def list_owned(user_id: str, job_kind: str | None = None) -> dict:
    return {'schedules': service.list_schedules(user_id, job_kind)}


@tool('schedule.update', risk_level='self_write')
@validation_errors
def update(user_id: str, schedule_id: int, patch: dict) -> dict:
    return service.propose(user_id, 'update', {'id': schedule_id, 'patch': patch})


@tool('schedule.delete', risk_level='self_write')
@validation_errors
def delete(user_id: str, schedule_id: int) -> dict:
    return service.propose(user_id, 'delete', {'id': schedule_id})


@tool('schedule.pause', risk_level='self_write')
@validation_errors
def pause(user_id: str, schedule_id: int) -> dict:
    return {'schedule': service.mutate(user_id, 'pause', {'id': schedule_id})}


@tool('schedule.resume', risk_level='self_write')
@validation_errors
def resume(user_id: str, schedule_id: int) -> dict:
    return {'schedule': service.mutate(user_id, 'resume', {'id': schedule_id})}


@tool('schedule.run_now', risk_level='notify_self')
def run_now(user_id: str, schedule_id: int) -> dict:
    return runner.run_schedule(user_id, schedule_id, manual=True)
