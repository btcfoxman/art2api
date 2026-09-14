from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.catalog import normalize_request
from app.config import Settings
from app.db import Database
from app.main import create_app
from app.web_catalog import profiles


MARKER = {'X-Requested-With': 'art2api'}
MODEL = 'doubao-seedance-2-0-fast-260128'


@pytest.fixture
def config(tmp_path):
    return Settings(data_dir=tmp_path, api_key='a'*32, admin_token='b'*32,
                    encryption_key=Fernet.generate_key().decode())


def account(db, port=20001):
    value = db.save_account({'name': 'Console test', 'proxy_url': f'socks5://proxy:{port}', 'backend': 'web'})
    db.update_credentials(value['id'], {'web_cookie': 'test=value'})
    db.update_account(value['id'], status='ready', profiles=profiles())
    return db.save_account({'enabled': True}, value['id'])


def test_account_concurrency_has_no_twenty_limit_and_requires_positive_integer(config):
    app = create_app(config)
    with TestClient(app) as http:
        http.post('/login', data={'token': config.admin_token})
        response = http.post('/api/accounts', headers=MARKER, json={
            'name': 'Large account', 'proxy_url': 'socks5://proxy:20001', 'max_concurrency': 250})
        assert response.status_code == 200
        aid = response.json()['id']
        assert response.json()['max_concurrency'] == 250
        for value in (21, 1000, 1000000):
            response = http.patch('/api/accounts/'+aid, headers=MARKER, json={'max_concurrency': value})
            assert response.status_code == 200 and response.json()['max_concurrency'] == value
        for value in (0, -1, 1.5, True, '20'):
            assert http.patch('/api/accounts/'+aid, headers=MARKER, json={'max_concurrency': value}).status_code == 422
        assert app.state.db.account(aid)['max_concurrency'] == 1000000


def test_settings_access_validation_and_restart_persistence(config):
    original = replace(config)
    app = create_app(config)
    with TestClient(app) as http:
        assert http.patch('/api/settings', json={'queue_limit': 200}, headers=MARKER).status_code == 401
        http.post('/login', data={'token': config.admin_token})
        initial = http.get('/api/settings').json()
        assert http.patch('/api/settings', json={'queue_limit': 200}).status_code == 403
        for payload in ({'queue_limit': 0}, {'queue_limit': None}, {'queue_limit': True},
                        {'poll_interval_seconds': 0}, {'browser_timeout_seconds': 10},
                        {'admin_token': 'c'*32}, {'public_base_url': 'https://example.org'},
                        {'request_timeout_seconds': 30, 'task_timeout_seconds': 5}):
            assert http.patch('/api/settings', json=payload, headers=MARKER).status_code == 422
            assert http.get('/api/settings').json() == initial
        assert app.state.db.runtime_settings() == {}
        saved = http.patch('/api/settings', headers=MARKER, json={
            'poll_interval_seconds': 7, 'task_timeout_seconds': 1200,
            'request_timeout_seconds': 90, 'queue_limit': 200,
            'browser_timeout_seconds': 300, 'browser_headless': True,
            'sd25_video_policy': 'strict',
        }).json()
        assert saved['queue_limit'] == 200 and config.poll_interval == 7
        assert app.state.service.settings.task_timeout == 1200
        assert app.state.service.browsers.settings.browser_headless is True
        assert app.state.service.settings.sd25_video_policy == 'strict'
        assert all(secret not in str(saved) for secret in (config.api_key, config.admin_token, config.encryption_key))
    # Fresh environment values are overridden only for explicitly saved fields.
    with TestClient(create_app(original)) as http:
        http.post('/login', data={'token': original.admin_token})
        assert http.get('/api/settings').json() == saved


def test_task_pagination_is_bounded_and_totals_include_older_rows(config):
    app = create_app(config)
    with TestClient(app) as http:
        db = app.state.db
        aid = account(db)['id']
        request = normalize_request({'model': MODEL, 'prompt': 'sample'})
        ids = []
        for i in range(53):
            task, _ = db.create_task(request, [(aid, profiles()[MODEL])], str(i), 100)
            db.update_task(task['id'], status='failed' if i == 0 else 'succeeded')
            db.conn.execute('UPDATE tasks SET created_at=? WHERE id=?', (1000+i, task['id']))
            ids.append(task['id'])
        # Active task lies outside the first page; counts must still include it.
        db.update_task(ids[1], status='submission_unknown')
        assert http.get('/api/overview').status_code == 401
        http.post('/login', data={'token': config.admin_token})
        assert http.get('/api/overview').json() == {'total': 53, 'active': 1, 'succeeded': 51, 'failed': 1, 'unknown': 1}
        page1 = http.get('/api/tasks?limit=20').json()
        page2 = http.get('/api/tasks?limit=20&offset=20').json()
        page3 = http.get('/api/tasks?limit=20&offset=40').json()
        assert [t['id'] for t in page1+page2+page3] == list(reversed(ids))
        assert len(page1) == len(page2) == 20 and len(page3) == 13
        assert len(http.get('/api/tasks?limit=500').json()) == 53
        for query in ('limit=0', 'limit=501', 'offset=-1', 'limit=all'):
            assert http.get('/api/tasks?' + query).status_code == 422


def test_runtime_timeout_changes_only_new_tasks_and_keeps_idempotency(config):
    app = create_app(config)
    app.state.service.schedule = Mock()
    with TestClient(app) as http:
        db = app.state.db
        aid = account(db)['id']
        db.save_account({'max_concurrency': 3}, aid)
        http.post('/login', data={'token': config.admin_token})
        payload = {'model': MODEL, 'prompt': 'sample'}
        first = http.post('/api/tasks', json=payload, headers={**MARKER, 'Idempotency-Key': 'first'}).json()
        before = db.task(first['id'])
        http.patch('/api/settings', json={'task_timeout_seconds': 120}, headers=MARKER).raise_for_status()
        second = http.post('/api/tasks', json=payload, headers={**MARKER, 'Idempotency-Key': 'second'}).json()
        after = db.task(second['id'])
        assert before['query_deadline'] == before['created_at'] + 3600
        assert db.task(first['id'])['query_deadline'] == before['query_deadline']
        assert after['query_deadline'] == after['created_at'] + 120
        again = http.post('/api/tasks', json=payload, headers={**MARKER, 'Idempotency-Key': 'first'}).json()
        assert again['id'] == first['id'] and app.state.service.schedule.call_count == 2


def test_clear_completed_tasks_protects_other_states_and_preserves_channel_retries(config):
    app = create_app(config)
    app.state.service.start = AsyncMock()
    app.state.service.schedule = Mock()
    with TestClient(app) as http:
        db = app.state.db
        accounts = [account(db)['id'], account(db, 20002)['id']]
        for aid in accounts:
            db.save_account({'max_concurrency': 20}, aid)
        payload = {'model': MODEL, 'prompt': 'sample'}
        request = normalize_request(payload)
        protected_states = ['queued', 'preparing', 'submitting', 'running', 'submission_unknown', 'future_state']
        records = []
        for i in range(53):
            task, _ = db.create_task(request, [(accounts[i % 2], profiles()[MODEL])], str(i), 100)
            status = protected_states[i] if i < len(protected_states) else 'succeeded' if i % 2 else 'failed'
            db.update_task(task['id'], status=status, upstream_id=f'upstream-{i}',
                           result={'video_url': 'https://example.com/result.mp4'} if status == 'succeeded' else {},
                           error='generation failed' if status == 'failed' else '')
            records.append(db.task(task['id']))
        overview = db.task_summary()
        counts = [db.active_count(aid) for aid in accounts]
        assert http.delete('/api/tasks/completed', headers=MARKER).status_code == 401
        assert http.delete('/api/tasks/completed', headers={'Authorization': 'Bearer ' + config.api_key, **MARKER}).status_code == 401
        http.post('/login', data={'token': config.admin_token})
        assert http.delete('/api/tasks/completed').status_code == 403
        assert http.delete('/api/tasks/completed', headers={**MARKER, 'Origin': 'https://untrusted.example'}).status_code == 403
        assert db.task_summary() == overview
        assert len(http.get('/api/tasks?limit=20').json()) == 20

        assert http.delete('/api/tasks/completed', headers=MARKER).json() == {'cleared': 47}
        remaining = http.get('/api/tasks?limit=20').json()
        assert {t['id'] for t in remaining} == {t['id'] for t in records[:6]}
        assert {t['internal_status'] for t in remaining} == set(protected_states)
        assert http.get('/api/tasks?offset=20').json() == []
        assert http.get('/api/overview').json() == {'total': 6, 'active': 5, 'succeeded': 0, 'failed': 0, 'unknown': 1}
        assert [db.active_count(aid) for aid in accounts] == counts
        assert {t['id'] for t in db.tasks(active=True)} == {t['id'] for t in records[:5]}
        for task in records[:6]:
            assert db.task(task['id']) == task
        for task in records[6:]:
            stored = db.task(task['id'])
            assert stored['cleared_at'] > 0
            assert {**stored, 'cleared_at': 0} == task
        # Clearing the console must never turn an idempotent retry into a paid submission.
        for prefix in ('/v1/videos', '/api/v3/contents/generations/tasks'):
            for i in (6, 7):
                auth = {'Authorization': 'Bearer ' + config.api_key}
                original = http.get(prefix + '/' + records[i]['id'], headers=auth)
                assert original.status_code == 200
                again = http.post(prefix, json=payload, headers={**auth, 'Idempotency-Key': str(i)})
                assert again.status_code == 200 and again.json() == original.json()
                assert http.post(prefix, json={**payload, 'prompt': 'changed'}, headers={**auth, 'Idempotency-Key': str(i)}).status_code == 409
        app.state.service.schedule.assert_not_called()
        assert db.conn.execute('SELECT COUNT(*) FROM tasks').fetchone()[0] == 53
        assert http.delete('/api/tasks/completed', headers=MARKER).json() == {'cleared': 0}
        assert len([e for e in db.events() if e['kind'] == 'tasks_cleared']) == 1
        # Work finishing after the first clear is visible until the next clear.
        db.update_task(records[0]['id'], status='succeeded')
        assert db.task_summary()['succeeded'] == 1
        assert http.delete('/api/tasks/completed', headers=MARKER).json() == {'cleared': 1}


def test_clear_tasks_migrates_existing_database_and_survives_restart(config):
    path = config.data_dir / 'art2api.db'
    db = Database(path, config.encryption_key)
    aid = account(db)['id']
    request = normalize_request({'model': MODEL, 'prompt': 'sample'})
    task, _ = db.create_task(request, [(aid, profiles()[MODEL])], 'existing', 100)
    db.update_task(task['id'], status='succeeded')
    db.conn.execute('ALTER TABLE tasks DROP COLUMN cleared_at')
    db.close()
    db = Database(path, config.encryption_key)
    try:
        before = db.task(task['id'])
        assert before['cleared_at'] == 0 and len(db.tasks()) == 1
        assert db.clear_completed_tasks() == 1
    finally:
        db.close()
    db = Database(path, config.encryption_key)
    try:
        assert db.tasks() == [] and db.task_summary()['total'] == 0
        assert db.task(task['id'])['status'] == 'succeeded'
        replay, created = db.create_task(request, [], 'existing', 100)
        assert not created and replay['id'] == task['id']
        # A restored/nonterminal state can never be hidden by an old clear marker.
        db.update_task(task['id'], status='submission_unknown')
        assert db.clear_completed_tasks() == 0
        assert db.tasks()[0]['id'] == task['id']
        assert db.task_summary()['unknown'] == db.active_count(aid) == 1
    finally:
        db.close()
