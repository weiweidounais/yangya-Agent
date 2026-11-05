"""At-most-once intent claiming before user-visible effects; no external writes."""
import json
import logging
import time
import uuid
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app import db
from app.data_access import NotFound, user_scope
from app.data_access.session import SessionLocal
from app.agent.proactive import jobs, store
from app.agent.proactive.config import get_config
from .models import Schedule, ScheduleRun, now
from .service import LOCK, owned, serialize
from .triggers import next_run, utc


def _produce(schedule, cfg):
    kind = schedule['job_kind']
    if kind == 'custom_nl':
        from .custom import produce
        return produce(schedule)
    return {'title': schedule['name'], 'body': schedule['prompt_text']}


def run_schedule(uid, sid, *, manual=False, at=None, cfg=None):
    db.init_db()
    instant, cfg = at or now(), cfg or get_config()
    with LOCK:
        with SessionLocal() as session:
            row = owned(session, uid, sid)
            if not row.enabled or row.status != 'active':
                return {'ok': True, 'skipped': True, 'reason': 'inactive'}
            if not manual and (row.next_run_at is None or utc(row.next_run_at) > instant):
                return {'ok': True, 'skipped': True, 'reason': 'not_due'}
            if row.max_runs is not None and row.run_count >= row.max_runs:
                row.status, row.enabled, row.next_run_at = 'completed', False, None
                session.commit()
                return {'ok': True, 'skipped': True, 'reason': 'max_runs'}
            pkey = 'manual:' + uuid.uuid4().hex if manual else utc(row.next_run_at).isoformat()
            run = ScheduleRun(schedule_id=sid, user_id=uid, period_key=pkey, started_at=instant)
            session.add(run)
            row.run_count += 1
            row.last_run_at, row.last_status = instant, 'running'
            try:
                session.commit()  # Durable claim precedes any effects.
            except IntegrityError:
                session.rollback()
                return {'ok': True, 'skipped': True, 'reason': 'already_claimed'}
            session.refresh(run)
            run_id, schedule = run.id, serialize(row)
        item, detail, ok = None, {}, False
        try:
            content = _produce(schedule, cfg)
            if content:
                detail.update(content.get('detail', {}))
            if content and content.get('skipped'):
                detail['skipped'] = content['skipped']
            elif content:
                payload = {'schedule_id': sid, 'suggest_only': True}
                item = store.create_item(uid, schedule['job_kind'], title=content['title'], body=content['body'],
                                         payload=payload, period_key=f'schedule:{sid}:{pkey}')
                detail['item_id'] = item['id']
                delivery = {'ok': False, 'skipped': True, 'reason': 'in_app_only'}
                from .delivery import channels, email_to_owner
                destinations = channels(schedule['delivery'])
                if 'channel' in destinations:
                    try:
                        delivery = jobs._try_deliver(uid, content['title'] + '\n' + content['body'],
                                                     kind=schedule['job_kind'], channel_preference=schedule['channel_preference'])
                    except Exception:
                        delivery = {'ok': False, 'skipped': True, 'reason': 'channel_unavailable'}
                detail['delivery'] = {k: delivery.get(k) for k in ('ok', 'skipped', 'reason', 'channel')}
                if 'email' in destinations:
                    try:
                        email = email_to_owner(uid, content['title'], content['body'])
                    except Exception:
                        email = {'ok': False, 'reason': 'email_failed'}
                    detail['email'] = {k: email.get(k) for k in ('ok', 'skipped', 'reason')}
                    if not email.get('ok'):
                        detail['skipped'] = email.get('reason') or 'email_failed'
                # Keep the item pending for the in-app inbox even when IM succeeds.
            else:
                detail['summary'] = '本次没有需要提醒的内容'
            ok = True
        except Exception as exc:
            # Do not persist provider errors or message contents in an error field;
            # the operator log keeps the exception type only.
            logging.getLogger(__name__).warning('schedule %s run failed: %s', sid, type(exc).__name__)
            detail['error'] = '任务执行失败，请稍后手动重试'
        with SessionLocal() as session, session.begin():
            row = owned(session, uid, sid)
            run = session.get(ScheduleRun, run_id)
            run.ok, run.finished_at = ok, now()
            run.detail_json, run.item_id = json.dumps(detail, ensure_ascii=False), item['id'] if item else None
            row.success_count += int(ok)
            row.fail_count += int(not ok)
            row.last_status, row.last_error = ('success' if ok else 'failed'), detail.get('error', '')
            trigger = json.loads(row.trigger_json)
            if row.max_runs is not None and row.run_count >= row.max_runs:
                row.enabled, row.status, row.next_run_at = False, 'completed', None
            elif ok and trigger['type'] == 'once':
                row.enabled, row.status, row.next_run_at = False, 'completed', None
            elif trigger['type'] == 'once':
                row.next_run_at = instant + timedelta(seconds=60)
            elif not manual:
                row.next_run_at = next_run(trigger, row.tz, instant)
            row.updated_at = now()
        return {'ok': ok, 'run_id': run_id, **detail}


def run_due(cfg, *, started=None, at=None):
    started = time.perf_counter() if started is None else started
    instant = at or now()
    db.init_db()
    from .activity import pause_idle
    pause_idle(instant)
    with SessionLocal() as session:
        due = [(r.user_id, r.id) for r in session.scalars(select(Schedule).where(
            Schedule.enabled.is_(True), Schedule.status == 'active', Schedule.next_run_at <= instant
        ).order_by(Schedule.next_run_at, Schedule.id).limit(cfg.max_users_per_tick))]
    out = []
    for uid, sid in due:
        if time.perf_counter() - started >= cfg.soft_budget_seconds:
            break
        with user_scope(uid):
            try:
                result = run_schedule(uid, sid, at=instant, cfg=cfg)
            except NotFound:
                # The due list is a snapshot; deletion may win before execution.
                result = {'ok': True, 'skipped': True, 'reason': 'deleted'}
            out.append(result)
    return out
