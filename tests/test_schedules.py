"""Phase 1 schedule coverage, using only synthetic users and isolated test DBs."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select, delete
from app import db
from app.data_access import NotFound, user_scope, system_scope
from app.data_access.session import SessionLocal
from app.agent.schedules import service, runner
from app.agent.schedules.models import Schedule, ScheduleRun, ScheduleProposal
from app.agent.schedules.triggers import next_run
from app.agent.proactive import jobs, store
from app.agent.proactive.config import ProactiveConfig

UTC = timezone.utc


@pytest.fixture(autouse=True)
def isolated_schedule_rows(uid):
    yield
    # The shared harness DB persists across tests; global ticks must not inherit
    # an earlier synthetic user's overdue or interrupted schedules.
    with user_scope(uid), SessionLocal() as session, session.begin():
        for model in (ScheduleRun, ScheduleProposal, Schedule):
            session.execute(delete(model).where(model.user_id == uid))


def payload(kind='custom_nl', **patch):
    return {'name': '合成测试任务', 'job_kind': kind, 'trigger': {'type': 'cron', 'expr': '0 8 * * *'},
            'prompt_text': '合成指令：汇总今天的公开资讯', **patch}


def once():
    return payload('one_shot_reminder', trigger={'type': 'once', 'run_at': (datetime.now(UTC) + timedelta(hours=1)).isoformat()}, prompt_text='合成提醒：提交本周周报')


def custom_content(monkeypatch, content):
    """custom_nl is the only recurring kind; stub its read-only planning pass."""
    from app.agent.schedules import custom
    produce = Mock(return_value=content)
    monkeypatch.setattr(custom, 'produce', produce)
    return produce


def due(uid, sid):
    instant = datetime.now(UTC) - timedelta(seconds=1)
    with SessionLocal() as session, session.begin():
        service.owned(session, uid, sid).next_run_at = instant
    return instant


def test_crud_ownership_soft_delete(uid):
    row = service.mutate(uid, 'create', payload())
    assert row['tz'] == 'Asia/Shanghai'
    assert row['run_count'] == row['success_count'] == row['fail_count'] == 0
    assert service.get_schedule(uid, row['id'])['name'] == '合成测试任务'
    updated = service.mutate(uid, 'update', {'id': row['id'], 'patch': {'name': '新名称'}})
    assert updated['name'] == '新名称'
    with user_scope('synthetic-other'):
        assert service.list_schedules('synthetic-other') == []
        for fn in (lambda: service.get_schedule('synthetic-other', row['id']),
                   lambda: service.runs('synthetic-other', row['id']),
                   lambda: service.mutate('synthetic-other', 'delete', {'id': row['id']})):
            with pytest.raises(NotFound):
                fn()
        with SessionLocal() as session:
            assert session.get(Schedule, row['id']) is None
    service.mutate(uid, 'delete', {'id': row['id']})
    assert service.list_schedules(uid) == []
    with SessionLocal() as session:
        assert session.get(Schedule, row['id']).status == 'deleted'


def test_timezone_cron_once_and_interval():
    at = datetime(2026, 10, 17, 0, 1, tzinfo=UTC)
    assert next_run({'type': 'cron', 'expr': '0 8 * * *'}, 'Asia/Shanghai', at) == datetime(2026, 10, 18, 0, tzinfo=UTC)
    assert next_run({'type': 'once', 'run_at': '2026-10-17T15:00:00'}, 'Asia/Shanghai', at) == datetime(2026, 10, 17, 7, tzinfo=UTC)
    assert next_run({'type': 'interval', 'every_seconds': 120}, 'Asia/Shanghai', at) == at + timedelta(seconds=120)
    # DOM and DOW use standard cron OR when both are restricted.
    assert next_run({'type': 'cron', 'expr': '*/15 8-9 20 10 0'}, 'Asia/Shanghai', at) == datetime(2026, 10, 18, 0, tzinfo=UTC)
    # Nonexistent spring-forward 02:30 is omitted, not silently shifted.
    assert next_run({'type': 'cron', 'expr': '30 2 * * *'}, 'America/New_York', datetime(2026, 3, 8, 0, tzinfo=UTC)) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize('expr,equivalent,expected_day', [
    ('0 8 */1 * 1-5', '0 8 * * 1-5', 19),
    ('0 8 20 * */1', '0 8 20 * *', 20),
])
def test_cron_unit_step_preserves_wildcard_semantics(expr, equivalent, expected_day):
    # Saturday after 08:00: weekdays must skip Sunday, and DOM must stay active.
    at = datetime(2026, 10, 17, 0, 1, tzinfo=UTC)
    expected = datetime(2026, 10, expected_day, 0, tzinfo=UTC)
    for _ in range(8):
        actual = next_run({'type': 'cron', 'expr': expr}, 'Asia/Shanghai', at)
        assert actual == next_run({'type': 'cron', 'expr': equivalent}, 'Asia/Shanghai', at)
        if at.day == 17:
            assert actual == expected
        at = actual


@pytest.mark.parametrize('expr,expected_day', [
    ('0 8 */2 * 0', 25),  # Odd DOM AND Sunday; October 18 is excluded.
    ('0 8 20 * */2', 20),  # DOM 20 AND Sunday/Tuesday/Thursday/Saturday.
    ('0 8 1-31 * 1-5', 18),  # Explicit ranges still use OR.
    ('0 8 20 * 0-7', 18),
])
def test_cron_stepped_wildcards_and_explicit_ranges(expr, expected_day):
    at = datetime(2026, 10, 17, 0, 1, tzinfo=UTC)
    assert next_run({'type': 'cron', 'expr': expr}, 'Asia/Shanghai', at) == datetime(2026, 10, expected_day, 0, tzinfo=UTC)


@pytest.mark.parametrize('trigger', [{'type': 'cron', 'expr': '60 8 * * *'}, {'type': 'cron', 'expr': '* * * *'}, {'type': 'cron', 'expr': '*/0 * * * *'}, {'type': 'interval', 'every_seconds': 0}, {'type': 'event'}, {'type': 'once', 'run_at': 'tomorrow'}])
def test_invalid_triggers(trigger):
    with pytest.raises(ValueError):
        next_run(trigger, 'Asia/Shanghai', datetime.now(UTC))


def test_profile_timezone(uid):
    db.save_profile(uid, {'tz': 'America/New_York'})
    assert service.mutate(uid, 'create', payload())['tz'] == 'America/New_York'


def test_cap_create_resume_and_update(uid):
    rows = [service.mutate(uid, 'create', payload()) for _ in range(10)]
    with pytest.raises(ValueError, match='10'):
        service.mutate(uid, 'create', payload())
    service.mutate(uid, 'pause', {'id': rows[0]['id']})
    service.mutate(uid, 'create', payload())
    for op, extra in [('resume', {}), ('update', {'patch': {'enabled': True}})]:
        with pytest.raises(ValueError, match='10'):
            service.mutate(uid, op, {'id': rows[0]['id'], **extra})
    # Editing an existing active row at the cap is allowed.
    assert service.mutate(uid, 'update', {'id': rows[1]['id'], 'patch': {'name': '新名称'}})['name'] == '新名称'


def test_pause_deleted_completed_never_execute(uid):
    row = service.mutate(uid, 'create', once())
    due(uid, row['id'])
    service.mutate(uid, 'pause', {'id': row['id']})
    assert runner.run_due(ProactiveConfig(enabled=True)) == []
    assert runner.run_schedule(uid, row['id'], manual=True)['reason'] == 'inactive'
    assert service.runs(uid, row['id']) == []
    service.mutate(uid, 'delete', {'id': row['id']})
    assert runner.run_due(ProactiveConfig(enabled=True)) == []


def test_once_completes_and_records_inbox_without_binding(uid):
    row = service.mutate(uid, 'create', once())
    due(uid, row['id'])
    result = runner.run_due(ProactiveConfig(enabled=True))
    assert len(result) == 1 and result[0]['ok']
    assert result[0]['delivery']['reason'] == 'no_binding'
    updated = service.get_schedule(uid, row['id'])
    assert (updated['status'], updated['enabled'], updated['next_run_at']) == ('completed', False, None)
    assert (updated['run_count'], updated['success_count'], updated['fail_count']) == (1, 1, 0)
    run = service.runs(uid, row['id'])[0]
    assert run['ok'] and run['finished_at'] and run['item_id']
    item = store.list_pending(uid)[0]
    assert item['id'] == run['item_id'] and item['body'] == once()['prompt_text']
    assert item['payload']['schedule_id'] == row['id']
    assert runner.run_due(ProactiveConfig(enabled=True)) == []
    assert runner.run_schedule(uid, row['id'], manual=True)['reason'] == 'inactive'


def test_channel_failure_is_best_effort_and_inbox_written_first(uid, monkeypatch):
    row = service.mutate(uid, 'create', once())
    def broken(*a, **kw):
        assert store.list_pending(uid)[0]['payload']['schedule_id'] == row['id']
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(jobs, '_try_deliver', broken)
    result = runner.run_schedule(uid, row['id'], manual=True)
    assert result['ok'] and result['delivery']['reason'] == 'channel_unavailable'
    assert service.get_schedule(uid, row['id'])['success_count'] == 1


def test_in_app_only_never_calls_channel(uid, monkeypatch):
    deliver = Mock(side_effect=AssertionError('unexpected channel'))
    monkeypatch.setattr(jobs, '_try_deliver', deliver)
    row = service.mutate(uid, 'create', {**once(), 'delivery': 'in_app'})
    assert runner.run_schedule(uid, row['id'], manual=True)['ok']
    deliver.assert_not_called()


def test_failure_accounted_and_can_retry_once(uid, monkeypatch):
    row = service.mutate(uid, 'create', once())
    with monkeypatch.context() as m:
        m.setattr(runner, '_produce', Mock(side_effect=RuntimeError('synthetic provider detail')))
        assert not runner.run_schedule(uid, row['id'], manual=True)['ok']
    failed = service.get_schedule(uid, row['id'])
    assert failed['status'] == 'active' and failed['fail_count'] == 1
    assert 'provider' not in failed['last_error']
    assert runner.run_schedule(uid, row['id'], manual=True)['ok']
    assert service.get_schedule(uid, row['id'])['run_count'] == 2


def test_claim_idempotency_and_concurrency(uid):
    row = service.mutate(uid, 'create', once())
    instant = due(uid, row['id'])
    def run():
        with user_scope(uid):
            return runner.run_schedule(uid, row['id'], at=instant + timedelta(seconds=1))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(not r.get('skipped', False) for r in results) == 1
    assert len(service.runs(uid, row['id'])) == len(store.list_pending(uid)) == 1


def test_durable_claim_blocks_repeat(uid):
    row = service.mutate(uid, 'create', once())
    instant = due(uid, row['id'])
    with SessionLocal() as session, session.begin():
        session.add(ScheduleRun(schedule_id=row['id'], user_id=uid, period_key=instant.isoformat()))
    assert runner.run_schedule(uid, row['id'])['reason'] == 'already_claimed'
    assert store.list_pending(uid) == []
    assert service.get_schedule(uid, row['id'])['run_count'] == 0


def test_tick_overrides_and_records_user_runs(uid, monkeypatch):
    row = service.mutate(uid, 'create', payload(trigger={'type': 'interval', 'every_seconds': 3600}))
    due(uid, row['id'])
    monkeypatch.setattr(jobs, 'list_candidate_user_ids', lambda **kw: [uid, 'synthetic-default'])
    produce = custom_content(monkeypatch, {'title': '合成汇总', 'body': '合成内容'})
    with system_scope():
        result = jobs.run_tick(ProactiveConfig(enabled=True))
    assert result['users'] == 2
    assert len(result['schedules']) == 1 and result['schedules'][0]['ok']
    assert produce.call_args.args[0]['user_id'] == uid
    assert store.list_pending(uid)[0]['body'] == '合成内容'
    updated = service.get_schedule(uid, row['id'])
    assert updated['run_count'] == 1 and updated['next_run_at'] > updated['last_run_at']
    with SessionLocal() as session:
        assert session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == row['id'])).one().ok


def test_deleted_queued_schedule_does_not_abort_tick(uid, monkeypatch):
    deleted = service.mutate(uid, 'create', once())
    following = service.mutate(uid, 'create', once())
    for row in (deleted, following):
        due(uid, row['id'])
    execute = runner.run_schedule

    def delete_before_execution(user, sid, **kwargs):
        if sid == deleted['id']:
            service.mutate(user, 'delete', {'id': sid})
        return execute(user, sid, **kwargs)

    monkeypatch.setattr(runner, 'run_schedule', delete_before_execution)
    monkeypatch.setattr(jobs, 'list_candidate_user_ids', lambda **kw: [uid])
    with system_scope():
        result = jobs.run_tick(ProactiveConfig(enabled=True))
    assert result['schedules'][0] == {'ok': True, 'skipped': True, 'reason': 'deleted'}
    assert result['schedules'][1]['ok'] and result['schedules'][1]['item_id']
    assert service.get_schedule(uid, following['id'])['status'] == 'completed'
    assert len(service.runs(uid, following['id'])) == 1
    with SessionLocal() as session:
        assert list(session.scalars(select(ScheduleRun).where(ScheduleRun.schedule_id == deleted['id']))) == []


def test_custom_interval_without_content_skips_inbox(uid, monkeypatch):
    row = service.mutate(uid, 'create', payload(trigger={'type': 'interval', 'every_seconds': 600}))
    at = due(uid, row['id']) + timedelta(seconds=1)
    content = custom_content(monkeypatch, None)
    result = runner.run_schedule(uid, row['id'], at=at)
    assert result['ok'] and result['summary'] == '本次没有需要提醒的内容'
    assert service.get_schedule(uid, row['id'])['next_run_at'] == (at + timedelta(seconds=600)).isoformat()
    assert service.overrides(uid, 'custom_nl')
    content.assert_called_once()
    assert store.list_pending(uid) == []


def test_budget_and_max_runs(uid, monkeypatch):
    row = service.mutate(uid, 'create', payload(max_runs=1))
    due(uid, row['id'])
    custom_content(monkeypatch, {'title': '合成汇总', 'body': '合成内容'})
    assert runner.run_due(ProactiveConfig(enabled=True, soft_budget_seconds=.01), started=time.perf_counter() - 1) == []
    assert runner.run_schedule(uid, row['id'], manual=True)['ok']
    assert service.get_schedule(uid, row['id'])['status'] == 'completed'


def test_manual_run_preserves_recurring_time(uid, monkeypatch):
    row = service.mutate(uid, 'create', payload())
    custom_content(monkeypatch, {'title': '合成汇总', 'body': '测试'})
    assert runner.run_schedule(uid, row['id'], manual=True)['ok']
    assert service.get_schedule(uid, row['id'])['next_run_at'] == row['next_run_at']


def test_tools_propose_only_and_force_session_owner(uid):
    from app.agent.tool_registry import invoke_tool
    result = invoke_tool('schedule.create', {'user_id': 'synthetic-forged', 'schedule': payload()})
    assert result['needs_confirm'] and service.list_schedules(uid) == []
    proposal = result['proposal']
    assert proposal['user_id'] == uid
    with user_scope('synthetic-other'), pytest.raises(NotFound):
        service.decide('synthetic-other', proposal['id'], True)
    row = service.decide(uid, proposal['id'], True)['schedule']
    assert row['created_by'] == 'agent'
    with pytest.raises(ValueError, match='已经处理'):
        service.decide(uid, proposal['id'], True)
    for tool, args in [('update', {'patch': {'name': '变更'}}), ('delete', {})]:
        proposal = invoke_tool(f'schedule.{tool}', {'schedule_id': row['id'], **args})['proposal']
        assert service.get_schedule(uid, row['id'])['name'] == row['name']
        service.decide(uid, proposal['id'], False)
    assert invoke_tool('schedule.pause', {'schedule_id': row['id']})['schedule']['status'] == 'paused'
    assert invoke_tool('schedule.resume', {'schedule_id': row['id']})['schedule']['status'] == 'active'
    assert len(invoke_tool('schedule.list', {})['schedules']) == 1
    refused = invoke_tool('schedule.create', {'schedule': payload(), 'confirmed': True})
    assert refused['error']['code'] == 'schema_validation'


def test_api_crud_and_cross_user_404(client, auth, uid):
    headers = auth(uid)
    response = client.post('/api/schedules', json=once(), headers=headers)
    assert response.status_code == 200, response.text
    row = response.json()['data']
    sid = row['id']
    for method, suffix, body in [('GET', '', None), ('GET', '/runs', None), ('PATCH', '', {'name': '偷改'}), ('DELETE', '', None), ('POST', '/pause', None), ('POST', '/resume', None), ('POST', '/run_now', None)]:
        kwargs = {'headers': auth('synthetic-api-other')}
        if body is not None:
            kwargs['json'] = body
        assert client.request(method, f'/api/schedules/{sid}{suffix}', **kwargs).status_code == 404
    assert client.patch(f'/api/schedules/{sid}', json={'name': '新名称'}, headers=headers).json()['data']['name'] == '新名称'
    assert client.post(f'/api/schedules/{sid}/pause', headers=headers).json()['data']['status'] == 'paused'
    assert client.post(f'/api/schedules/{sid}/resume', headers=headers).json()['data']['status'] == 'active'
    assert client.post(f'/api/schedules/{sid}/run_now', headers=headers).json()['data']['ok']
    assert client.get(f'/api/schedules/{sid}/runs?limit=1', headers=headers).json()['data'][0]['ok']
    listing = client.get('/api/schedules', headers=headers).json()['data']
    assert listing['stats'] == {'run_count': 1, 'success_count': 1, 'fail_count': 0}
    assert client.delete(f'/api/schedules/{sid}', headers=headers).status_code == 200
    assert client.get('/api/schedules', headers=headers).json()['data']['schedules'] == []
    assert client.get(f'/api/schedules/{sid}', headers=headers).status_code == 404


def test_api_no_auth_and_cookie_auth(auth, uid):
    from tests.conftest import RawTestClient
    from app.main import app
    from app.auth import mint_user_token
    with RawTestClient(app) as client:
        assert client.get('/api/schedules').status_code == 401
        assert client.post('/api/schedules', json=payload()).status_code == 401
        client.cookies.set('ja_session', mint_user_token(uid))
        assert client.get('/api/schedules').status_code == 200


def test_api_model_chat_confirm_flow(client, auth, uid, monkeypatch):
    from app.gateway import llm_gateway
    from app.agent import router
    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: True)
    def model(system, user, **kwargs):
        if 'Select one tool' in system:
            return {'tool': 'schedule.create', 'confidence': .99}
        return {'args': {'schedule': payload()}}
    monkeypatch.setattr(llm_gateway, 'chat_json', model)
    response = client.post('/api/chat', json={'message': '每天早上八点帮我汇总科技新闻'}, headers=auth(uid))
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['status'] == 'schedules'
    proposal = data['data']['schedule_proposal']
    assert service.list_schedules(uid) == []
    path = f"/api/schedules/proposals/{proposal['id']}/decide"
    assert client.post(path, json={'confirmed': True}, headers=auth('synthetic-other')).status_code == 404
    assert client.post(path, json={'confirmed': True}, headers=auth(uid)).status_code == 200
    assert len(service.list_schedules(uid)) == 1
    assert client.post(path, json={'confirmed': True}, headers=auth(uid)).status_code == 422


def test_chat_missing_time_asks_without_mutation(uid, monkeypatch):
    from app.agent.schedules.chat import respond
    from app.gateway import llm_gateway
    monkeypatch.setattr(llm_gateway, 'chat_json', lambda *a, **kw: {'question': '希望几点提醒？'})
    assert respond(uid, 'schedule.create', '明天提醒我')['messages'][0]['content'] == '希望几点提醒？'
    assert service.list_schedules(uid) == []


def test_chat_handler_checkpoints_schedule_and_completes_clarification(uid, monkeypatch):
    """Exercise the real handler/checkpoint without the HTTP fixture's portal."""
    from app.main import ChatRequest, chat
    from app.gateway import llm_gateway
    from app.graph import get_thread_snapshot

    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: True)
    def model(system, user, **kwargs):
        data = json.loads(user)
        if 'Select one tool' in system:
            return {'tool': 'schedule.create' if data['message'] == '明天提醒我交周报' else None, 'confidence': .99}
        if data['message'] == '明天提醒我交周报':
            return {'question': '希望几点提醒？', 'args': {'schedule': {'prompt_text': '交周报'}}}
        assert data['message'] == '下午三点'
        assert data['pending']['turns'][0]['content'] == '明天提醒我交周报'
        assert data['pending']['args']['schedule']['prompt_text'] == '交周报'
        return {'args': {'schedule': once()}}
    monkeypatch.setattr(llm_gateway, 'chat_json', model)
    first = chat(ChatRequest(user_id=uid, message='明天提醒我交周报'))
    assert get_thread_snapshot(first.thread_id)['user_id'] == uid
    second = chat(ChatRequest(user_id=uid, thread_id=first.thread_id, message='下午三点'))
    assert second.data['schedule_proposal']
    assert get_thread_snapshot(first.thread_id)['values']['pending_schedule'] is None
    assert service.list_schedules(uid) == []
    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: False)
    third = chat(ChatRequest(user_id=uid, thread_id=first.thread_id, message='你好'))
    assert third.thread_id == first.thread_id


@pytest.mark.parametrize('first_tool', ['schedule.create', 'schedule.list'])
def test_schedule_first_chat_preserves_owned_thread(client, auth, uid, monkeypatch, first_tool):
    from app.gateway import llm_gateway
    from app.graph import get_thread_snapshot

    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: True)
    def model(system, user, **kwargs):
        if 'Select one tool' in system:
            return {'tool': first_tool, 'confidence': .99}
        return {'args': {'schedule': payload()}}
    monkeypatch.setattr(llm_gateway, 'chat_json', model)
    first = client.post('/api/chat', json={'message': '查看或创建合成定时任务'}, headers=auth(uid))
    assert first.status_code == 200
    tid = first.json()['thread_id']
    assert get_thread_snapshot(tid)['user_id'] == uid
    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: False)
    second = client.post('/api/chat', json={'thread_id': tid, 'message': '你好'}, headers=auth(uid))
    assert second.status_code == 200, second.text
    assert second.json()['thread_id'] == tid
    foreign = client.post('/api/chat', json={'thread_id': tid, 'message': '你好'}, headers=auth('synthetic-other'))
    assert foreign.status_code == 404


def test_schedule_clarification_retains_context_until_confirm(client, auth, uid, monkeypatch):
    from app.gateway import llm_gateway
    from app.graph import get_thread_snapshot

    partial = {'schedule': {'name': '交周报', 'job_kind': 'one_shot_reminder', 'prompt_text': '交周报'}}
    completed = once()
    requests = []
    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: True)
    def model(system, user, **kwargs):
        data = json.loads(user)
        if 'Select one tool' in system:
            return {'tool': 'schedule.create' if data['message'] == '明天提醒我交周报' else None, 'confidence': .99}
        requests.append(data)
        if data['message'] == '明天提醒我交周报':
            return {'args': partial, 'question': '希望几点提醒？'}
        assert data['pending']['args'] == partial
        assert data['pending']['turns'] == [
            {'role': 'user', 'content': '明天提醒我交周报'},
            {'role': 'assistant', 'content': '希望几点提醒？'},
        ]
        return {'args': {'schedule': completed}}
    monkeypatch.setattr(llm_gateway, 'chat_json', model)
    first = client.post('/api/chat', json={'message': '明天提醒我交周报'}, headers=auth(uid))
    assert first.status_code == 200
    tid = first.json()['thread_id']
    assert first.json()['data']['messages'][0]['content'] == '希望几点提醒？'
    assert service.list_schedules(uid) == []
    second = client.post('/api/chat', json={'thread_id': tid, 'message': '下午三点'}, headers=auth(uid))
    assert second.status_code == 200, second.text
    proposal = second.json()['data']['schedule_proposal']
    assert requests[-1]['message'] == '下午三点'
    assert get_thread_snapshot(tid)['values']['pending_schedule'] is None
    assert service.list_schedules(uid) == []
    confirmed = client.post(f"/api/schedules/proposals/{proposal['id']}/decide", json={'confirmed': True}, headers=auth(uid))
    assert confirmed.status_code == 200
    assert len(service.list_schedules(uid)) == 1


def test_unrelated_chat_clears_schedule_clarification(client, auth, uid, monkeypatch):
    from app.gateway import llm_gateway
    from app.graph import get_thread_snapshot

    monkeypatch.setattr(llm_gateway, 'has_llm', lambda *a: True)
    def model(system, user, **kwargs):
        data = json.loads(user)
        if 'Select one tool' in system:
            return {'tool': 'schedule.create' if data['message'] == '明天提醒我' else None, 'confidence': .99}
        return {'unrelated': True} if data.get('pending') else {'question': '希望几点提醒？'}
    monkeypatch.setattr(llm_gateway, 'chat_json', model)
    monkeypatch.setattr(llm_gateway, 'chat_text', lambda *a, **kw: '你好')
    first = client.post('/api/chat', json={'message': '明天提醒我'}, headers=auth(uid))
    tid = first.json()['thread_id']
    second = client.post('/api/chat', json={'thread_id': tid, 'message': '先不设了，聊聊周末安排'}, headers=auth(uid))
    assert second.status_code == 200, second.text
    assert 'schedule_proposal' not in second.json()['data']
    assert get_thread_snapshot(tid)['values']['pending_schedule'] is None
    assert service.list_schedules(uid) == []


@pytest.mark.parametrize('operation', ['create', 'resume'])
def test_cap_errors_through_registry_and_chat(uid, monkeypatch, operation):
    from app.agent.tool_registry import invoke_tool
    from app.agent.schedules.chat import respond
    from app.gateway import llm_gateway

    paused = service.mutate(uid, 'create', payload(enabled=False))
    for _ in range(10):
        service.mutate(uid, 'create', payload())
    args = {'schedule': payload()} if operation == 'create' else {'schedule_id': paused['id']}
    name = f'schedule.{operation}'
    result = invoke_tool(name, args)
    assert result['ok'] is False
    assert result['error'] == {
        'code': 'schedule_validation',
        'message': '最多同时启用 10 个定时任务，请先冻结或删除一个',
    }
    monkeypatch.setattr(llm_gateway, 'chat_json', lambda *a, **kw: {'args': args})
    response = respond(uid, name, '合成测试：建立或恢复这个定时任务')
    assert response['messages'][0]['content'] == result['error']['message']
    assert 'schedule_proposal' not in response
    assert service.get_schedule(uid, paused['id'])['status'] == 'paused'
    assert sum(row['enabled'] for row in service.list_schedules(uid)) == 10
    with SessionLocal() as session:
        assert list(session.scalars(select(ScheduleProposal).where(ScheduleProposal.user_id == uid))) == []


def test_chat_unknown_write_outcome_does_not_claim_success(uid, monkeypatch):
    from app.agent.schedules.chat import respond
    from app.gateway import llm_gateway

    monkeypatch.setattr(llm_gateway, 'chat_json', lambda *a, **kw: {'args': {'schedule': payload()}})
    def interrupted(*args, **kwargs):
        raise RuntimeError('synthetic interrupted write')
    monkeypatch.setattr(service, 'propose', interrupted)
    response = respond(uid, 'schedule.create', '合成测试：创建任务')
    assert response['messages'][0]['content'] == '结果尚未确认，请到顶部「定时」查看任务状态，不要重复操作。'
    assert 'schedule_proposal' not in response


@pytest.mark.parametrize('browser_tz', ['UTC', 'America/Los_Angeles'])
def test_schedule_ui_confirmation_persistence_and_timezone(browser_tz):
    import os
    import subprocess
    from pathlib import Path

    result = subprocess.run(
        ['node', 'tests/unit/schedules_ui.mjs'], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, 'TZ': browser_tz}, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
