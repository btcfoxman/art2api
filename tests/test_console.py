from dataclasses import replace
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.catalog import normalize_request
from app.config import Settings
from app.main import create_app
from app.web_catalog import profiles


MARKER = {'X-Requested-With': 'art2api'}
MODEL = 'doubao-seedance-2-0-fast-260128'


@pytest.fixture
def config(tmp_path):
    return Settings(data_dir=tmp_path, api_key='a'*32, admin_token='b'*32,
                    encryption_key=Fernet.generate_key().decode())


def account(db):
    value = db.save_account({'name': 'Console test', 'proxy_url': 'socks5://proxy:20001', 'backend': 'web'})
    db.update_credentials(value['id'], {'web_cookie': 'test=value'})
    db.update_account(value['id'], status='ready', profiles=profiles())
    return db.save_account({'enabled': True}, value['id'])


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
