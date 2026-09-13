import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.service import Service
from app.web import WebClient, web_task_context
from app.web_catalog import profiles


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, encryption_key=Fernet.generate_key().decode())
    db = Database(tmp_path/'test.db', settings.encryption_key)
    account = db.save_account({'name': 'web', 'proxy_url': 'socks5://xray:20001', 'backend': 'web'})
    aid = account['id']
    db.update_credentials(aid, {'web_cookie': 'session=private-cookie', 'web_user_id': 'user-1'})
    db.update_account(aid, profiles=profiles(), status='ready', egress_ip='203.0.113.4')
    db.save_account({'enabled': True}, aid)
    yield db, settings, aid
    db.close()


def factory_for(responder, proxies):
    def factory(proxy, timeout, **kwargs):
        proxies.append(proxy)
        return httpx.AsyncClient(transport=httpx.MockTransport(responder), **kwargs)
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize('procedure,post', [
    ('modelRouter.getCostQuote', True),
    ('userGenerationRouter.checkGenerationEligibility', True),
    ('uploadRouter.getPresignedUrlFromKey', True),
    ('userGenerationRouter.getUserGenerationById', False),
])
async def test_safe_rpc_recovers_through_same_proxy_without_leaking_secrets(setup, procedure, post):
    db, settings, aid = setup
    seen, proxies = [], []
    def responder(request):
        seen.append(request)
        if len(seen) == 1:
            raise httpx.ReadTimeout('private-cookie socks5://user:secret@proxy:20001 private-prompt', request=request)
        return httpx.Response(200, json={'result': {'data': {'json': {'id': 'ok'}}}})
    web = WebClient(aid, db, settings)
    async def backoff(_):
        assert not web.lock.locked()
    token = web_task_context.set('task-a')
    try:
        with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', AsyncMock(side_effect=backoff)) as sleep:
            assert await web.rpc(procedure, {'prompt': 'private-prompt'}, post=post) == {'id': 'ok'}
        sleep.assert_awaited_once_with(1)
    finally:
        web_task_context.reset(token)
    assert proxies == ['socks5://xray:20001']*2
    assert seen[0].content == seen[1].content and seen[0].url == seen[1].url
    events = db.events()
    assert [e['kind'] for e in events] == ['web_request_recovered', 'web_request_retry']
    assert all(e['task_id'] == 'task-a' for e in events)
    assert 'ReadTimeout' in events[1]['detail'] and procedure in events[1]['detail']
    assert not any(secret in json.dumps(events) for secret in ['private-cookie', 'user:secret', 'private-prompt'])


@pytest.mark.asyncio
async def test_safe_rpc_stops_after_three_attempts_with_diagnostic(setup):
    db, settings, aid = setup
    proxies = []
    def responder(request):
        raise httpx.ConnectError('secret-address', request=request)
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
        with pytest.raises(GatewayError) as error:
            await WebClient(aid, db, settings).rpc('modelRouter.getCostQuote', {}, post=True)
    assert len(proxies) == 3
    assert [c.args[0] for c in sleep.await_args_list] == [1, 2]
    assert error.value.code == 'proxy_error' and error.value.retryable
    assert 'ConnectError' in str(error.value) and '第 3 次' in str(error.value)
    assert 'secret-address' not in str(error.value)
    assert db.events()[0]['kind'] == 'web_request_failed'


@pytest.mark.asyncio
@pytest.mark.parametrize('procedure,post', [
    ('chatSession.createChatSession', True),
    ('userGenerationRouter.createUserGeneration', True),
    ('modelRouter.getModel', True),  # Unobserved method is not approved for replay.
])
async def test_mutations_and_unobserved_methods_never_replay(setup, procedure, post):
    db, settings, aid = setup
    proxies = []
    def responder(request):
        raise httpx.ReadTimeout('response lost', request=request)
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
        with pytest.raises(GatewayError):
            await WebClient(aid, db, settings).rpc(procedure, {}, post=post)
    assert len(proxies) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [400, 401, 403, 500, 'local_protocol'])
async def test_http_rejections_and_local_protocol_errors_are_not_transport_retried(setup, failure):
    db, settings, aid = setup
    proxies = []
    def responder(request):
        if failure == 'local_protocol':
            raise httpx.LocalProtocolError('invalid local header', request=request)
        return httpx.Response(failure, json={})
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
        with pytest.raises(GatewayError):
            await WebClient(aid, db, settings).rpc('modelRouter.getModel', {})
    assert len(proxies) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_stops_when_proxy_binding_changes(setup):
    db, settings, aid = setup
    proxies = []
    def responder(request):
        raise httpx.ConnectError('unavailable', request=request)
    async def backoff(_):
        db.save_account({'proxy_url': 'socks5://xray:20002'}, aid)
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', AsyncMock(side_effect=backoff)):
        with pytest.raises(GatewayError) as error:
            await WebClient(aid, db, settings).rpc('modelRouter.getModel', {})
    assert error.value.code == 'proxy_binding_changed'
    assert proxies == ['socks5://xray:20001']


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_submit_response', [False, True])
async def test_task_recovers_preflight_but_never_repeats_generation(setup, lose_submit_response):
    db, settings, aid = setup
    service = Service(db, settings)
    web = service.web(aid)
    seen, proxies = [], []
    def responder(request):
        procedure = request.url.path.rsplit('/', 1)[-1]
        seen.append(procedure)
        if procedure == 'modelRouter.getCostQuote' and seen.count(procedure) == 1:
            raise httpx.ConnectTimeout('preflight timeout', request=request)
        if procedure == 'userGenerationRouter.createUserGeneration' and lose_submit_response:
            raise httpx.ReadTimeout('submit response lost', request=request)
        return httpx.Response(200, json={'result': {'data': {'json': {'id': 'generation-1'}}}})
    async def prepare(task):
        await web.rpc('modelRouter.getCostQuote', {}, post=True)
        return {'prepared': True}
    web.prepare = prepare
    web.query = AsyncMock(return_value={'status': 'succeeded', 'video_url': 'https://media.example/result.mp4'})
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock):
        task = await service.create({'model': 'sd-2-5-480p', 'prompt': 'test'}, 'one-billable-submit')
        await asyncio.gather(*list(service.jobs.values()))
        await service.create({'model': 'sd-2-5-480p', 'prompt': 'test'}, 'one-billable-submit')
    assert db.task(task['id'])['status'] == ('submission_unknown' if lose_submit_response else 'succeeded')
    assert seen.count('userGenerationRouter.createUserGeneration') == 1
    assert seen.count('modelRouter.getCostQuote') == 2
    assert all(e['task_id'] == task['id'] for e in db.events() if e['kind'].startswith('web_request_'))
    assert web_task_context.get() is None
