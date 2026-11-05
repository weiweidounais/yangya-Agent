"""Owner-scoped CRUD and ask-first proposals. The MVP uses one process lock."""
import json
import threading
import uuid
from datetime import datetime
from sqlalchemy import select, func
from app import db
from app.data_access import NotFound
from app.data_access.session import SessionLocal
from .models import Schedule, ScheduleRun, ScheduleProposal, now
from .triggers import next_run, utc

LOCK = threading.RLock()
KINDS = {"one_shot_reminder": "一次性提醒", "custom_nl": "自定义指令"}
FIELDS = {"name", "job_kind", "trigger", "tz", "prompt_text", "delivery", "channel_preference", "max_runs", "enabled"}


def serialize(row):
    out = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, datetime):
            value = utc(value).isoformat()
        out[col.name] = value
    for key in ("trigger", "detail", "payload", "preview"):
        if key + '_json' in out:
            out[key] = json.loads(out.pop(key + '_json'))
    if isinstance(row, Schedule):
        out['paused_by_idle'] = row.status == 'paused_idle'
    return out


def owned(session, uid, sid):
    row = session.scalar(select(Schedule).where(Schedule.id == sid, Schedule.user_id == uid, Schedule.status != 'deleted'))
    if row is None:
        raise NotFound()
    return row


def list_schedules(uid, job_kind=None):
    db.init_db()
    if job_kind is not None and job_kind not in KINDS:
        raise ValueError("不支持的任务类型")
    with SessionLocal() as session:
        query = select(Schedule).where(Schedule.user_id == uid, Schedule.status != 'deleted')
        if job_kind is not None:
            query = query.where(Schedule.job_kind == job_kind)
        return [serialize(r) for r in session.scalars(query.order_by(Schedule.id.desc()))]


def get_schedule(uid, sid):
    db.init_db()
    with SessionLocal() as session:
        return serialize(owned(session, uid, sid))


def runs(uid, sid, limit=30):
    db.init_db()
    with SessionLocal() as session:
        owned(session, uid, sid)
        return [serialize(r) for r in session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == sid, ScheduleRun.user_id == uid).order_by(ScheduleRun.id.desc()).limit(max(1, min(limit, 100))))]


def validate(uid, patch, existing=None, at=None):
    if not isinstance(patch, dict) or set(patch) - FIELDS:
        raise ValueError("包含不支持的定时任务字段")
    data = {k: v for k, v in (existing or {}).items() if k in FIELDS}
    data.update(patch)
    data.setdefault('tz', (db.get_profile(uid) or {}).get('tz') or 'Asia/Shanghai')
    data.setdefault('delivery', 'both')
    data.setdefault('channel_preference', 'auto')
    data.setdefault('enabled', True)
    data.setdefault('prompt_text', '')
    data.setdefault('max_runs', None)
    if not isinstance(data.get('name'), str) or not 1 <= len(data['name'].strip()) <= 256:
        raise ValueError("任务名称须为 1 至 256 个字")
    data['name'] = data['name'].strip()
    if data.get('job_kind') not in KINDS:
        raise ValueError("不支持的任务类型")
    if type(data['enabled']) is not bool:
        raise ValueError("启用状态必须为布尔值")
    from .delivery import channels
    destinations = channels(data['delivery'])
    if not destinations or destinations - {'in_app', 'channel', 'email'} or data['channel_preference'] not in ('auto', 'feishu', 'wechat'):
        raise ValueError("不支持的推送方式或渠道")
    if destinations == {'in_app', 'channel'}:
        data['delivery'] = 'both'
    elif not isinstance(data['delivery'], str) or data['delivery'] not in ('in_app', 'both', 'channel', 'email'):
        data['delivery'] = ','.join(c for c in ('in_app', 'channel', 'email') if c in destinations)
    if not isinstance(data['prompt_text'], str) or len(data['prompt_text']) > 4000:
        raise ValueError("提醒内容须不超过 4000 个字")
    if data['job_kind'] == 'custom_nl' and not data['prompt_text'].strip():
        raise ValueError("自定义任务需要填写指令内容")
    if data['max_runs'] is not None and (type(data['max_runs']) is not int or data['max_runs'] < 1):
        raise ValueError("最大运行次数须为正整数")
    trigger = data.get('trigger')
    if isinstance(trigger, dict) and trigger.get('type') == 'event':
        raise ValueError("不支持事件触发")
    instant = at or now()
    nxt = next_run(trigger, data['tz'], instant)
    if data['job_kind'] == 'one_shot_reminder' and (trigger.get('type') != 'once' or not data['prompt_text'].strip()):
        raise ValueError("一次性提醒需要明确时间和提醒内容")
    if trigger.get('tz') and trigger['tz'] != data['tz']:
        raise ValueError("触发器时区与任务时区必须一致")
    changed_time = not existing or bool({'trigger', 'tz'} & patch.keys())
    if changed_time and trigger['type'] == 'once' and nxt <= instant:
        raise ValueError("一次性提醒时间必须在未来")
    data['next_run_at'] = nxt if changed_time else existing['next_run_at']
    if isinstance(data['next_run_at'], str):
        data['next_run_at'] = datetime.fromisoformat(data['next_run_at'])
    return data


def _cap(session, uid, exclude=None):
    query = select(func.count(Schedule.id)).where(Schedule.user_id == uid, Schedule.enabled.is_(True), Schedule.status == 'active')
    if exclude is not None:
        query = query.where(Schedule.id != exclude)
    if session.scalar(query) >= 10:
        raise ValueError("最多同时启用 10 个定时任务，请先冻结或删除一个")


def _apply(session, uid, operation, payload, created_by='user'):
    if operation == 'create':
        instant = now()
        data = validate(uid, payload, at=instant)
        if data['enabled']:
            _cap(session, uid)
        trigger = data.pop('trigger')
        row = Schedule(user_id=uid, **data, trigger_json=json.dumps(trigger), created_by=created_by,
                       status='active' if data['enabled'] else 'paused', created_at=instant, updated_at=instant)
        session.add(row)
    else:
        row = owned(session, uid, payload['id'])
        if operation == 'delete':
            row.status, row.enabled, row.next_run_at = 'deleted', False, None
        else:
            patch = payload.get('patch', {})
            if operation in ('pause', 'resume'):
                patch = {'enabled': operation == 'resume'}
            if row.status == 'completed' and patch.get('enabled') is True and 'trigger' not in patch:
                raise ValueError("已完成的任务需要设置新的触发时间")
            data = validate(uid, patch, serialize(row))
            if data['enabled']:
                if data['max_runs'] is not None and row.run_count >= data['max_runs']:
                    raise ValueError("已达到最大运行次数，请先调整次数限制")
                _cap(session, uid, row.id)
                if not row.enabled and not {'trigger', 'tz'} & patch.keys():
                    data['next_run_at'] = next_run(data['trigger'], data['tz'], now())
            for key, value in data.items():
                setattr(row, 'trigger_json' if key == 'trigger' else key, json.dumps(value) if key == 'trigger' else value)
            if row.status != 'completed' or data['enabled']:
                row.status = 'active' if data['enabled'] else ('paused_idle' if row.status == 'paused_idle' else 'paused')
        row.updated_at = now()
    session.flush()
    return serialize(row)


def mutate(uid, operation, payload, created_by='user'):
    db.init_db()
    with LOCK, SessionLocal() as session, session.begin():
        return _apply(session, uid, operation, payload, created_by)


def propose(uid, operation, payload):
    db.init_db()
    with LOCK, SessionLocal() as session, session.begin():
        if operation == 'create':
            preview = validate(uid, payload)
            _cap(session, uid)
        else:
            existing = serialize(owned(session, uid, payload['id']))
            preview = validate(uid, payload.get('patch', {}), existing) if operation == 'update' else existing
        preview = {**preview, 'kind_label': KINDS[preview['job_kind']]}
        row = ScheduleProposal(id=uuid.uuid4().hex, user_id=uid, operation=operation,
                               payload_json=json.dumps(payload, ensure_ascii=False),
                               preview_json=json.dumps(preview, ensure_ascii=False, default=lambda d: utc(d).isoformat()))
        session.add(row)
        session.flush()
        return {'proposal': serialize(row), 'needs_confirm': True}


def decide(uid, proposal_id, confirmed):
    db.init_db()
    with LOCK, SessionLocal() as session, session.begin():
        row = session.scalar(select(ScheduleProposal).where(ScheduleProposal.id == proposal_id, ScheduleProposal.user_id == uid).with_for_update())
        if row is None:
            raise NotFound()
        if row.status != 'pending':
            raise ValueError("这张确认卡已经处理，请查看定时任务面板")
        result = _apply(session, uid, row.operation, json.loads(row.payload_json), 'agent') if confirmed else None
        row.status = 'confirmed' if confirmed else 'rejected'
        return {'schedule': result, 'status': row.status}


def overrides(uid, kind):
    return any(s['enabled'] and s['status'] == 'active' and s['job_kind'] == kind for s in list_schedules(uid))
