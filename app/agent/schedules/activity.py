"""Authenticated heartbeat and atomic, once-per-idle-episode schedule pauses."""
import json
import threading
from datetime import timedelta

from sqlalchemy import select
from app import db
from app.data_access import user_scope
from app.data_access.session import SessionLocal
from app.agent.proactive.store import ProactiveItem
from .models import Schedule, UserActivity, now
from .service import LOCK
from .triggers import utc

ACTIVITY_LOCK = threading.RLock()


def heartbeat(uid):
    db.init_db()
    # API heartbeats must not wait for the runner's long-lived execution lock.
    with ACTIVITY_LOCK, SessionLocal() as session, session.begin():
        row = session.get(UserActivity, uid)
        if row is None:
            session.add(UserActivity(user_id=uid, last_active_at=now()))
        else:
            row.last_active_at = now()


def pause_idle(at):
    db.init_db()
    cutoff = at - timedelta(days=14)
    with SessionLocal() as session:
        users = list(session.scalars(select(Schedule.user_id).where(
            Schedule.enabled.is_(True), Schedule.status == 'active').distinct()))
    for uid in users:
        with user_scope(uid), LOCK, ACTIVITY_LOCK, SessionLocal() as session, session.begin():
            rows = list(session.scalars(select(Schedule).where(
                Schedule.user_id == uid, Schedule.enabled.is_(True), Schedule.status == 'active')))
            if not rows:
                continue
            activity = session.get(UserActivity, uid)
            if activity is None:
                # P2 did not track heartbeats: schedule age cannot prove inactivity.
                # Persist a conservative upgrade baseline once, then allow 14 days.
                session.add(UserActivity(user_id=uid, last_active_at=at))
                continue
            last_active = utc(activity.last_active_at)
            if last_active > cutoff:
                continue
            for row in rows:
                row.enabled, row.status, row.next_run_at = False, 'paused_idle', None
                row.updated_at = at
            session.add(ProactiveItem(
                user_id=uid, kind='schedules_idle_pause', title='定时任务已因长期未活跃暂停',
                body=f'你已连续 14 天没有聊天或 API 活动，已暂停 {len(rows)} 个定时任务。可在「定时」面板逐个恢复。',
                payload_json=json.dumps({'paused_by_idle': True, 'schedule_ids': [r.id for r in rows]}),
                period_key='idle:' + last_active.isoformat(), created_at=at, updated_at=at,
            ))
