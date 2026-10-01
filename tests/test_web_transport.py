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
from app.public_errors import GENERATION_FAILED, public_error
from app.web import WebClient, media_context, web_task_context
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
@pytest.mark.parametrize('failed_gets', [1, 3])
async def test_media_source_get_retries_before_any_upload(setup, failed_gets):
    db, settings, aid = setup
    stored = 'https://artlist-prod-ai-toolkit-custom-user-uploads.s3.eu-central-1.amazonaws.com/object'
    calls = []

    def responder(request):
        calls.append((request.method, request.url.host))
        if request.url.host == 'media.example':
            if sum(host == 'media.example' for _, host in calls) <= failed_gets:
                raise httpx.RemoteProtocolError('private download URL and proxy detail', request=request)
            return httpx.Response(200, content=b'image-content', headers={'content-type':'image/png'})
        if request.url.host == 'toolkit.artlist.io':
            name = request.url.path.rsplit('/', 1)[-1]
            value = ({'presignedUrl':stored+'?X-Amz-Signature=upload&x-id=PutObject',
                      'fileKey':'object'} if name == 'uploadRouter.getPresignedUrl' else
                     {'presignedUrl':stored+'?X-Amz-Signature=download&x-id=GetObject'})
            return httpx.Response(200, json={'result': {'data': {'json': value}}})
        return httpx.Response(200 if request.method == 'PUT' else 206, content=b'x')

    record = {}
    token = media_context.set(record)
    try:
        with (patch('app.web.client', factory_for(responder, [])),
              patch('app.web.probe', AsyncMock(return_value={
                  'streams':[{'codec_type':'video','width':1280,'height':720}]})),
              patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep):
            web = WebClient(aid, db, settings)
            try:
                if failed_gets == 3:
                    with pytest.raises(GatewayError) as error:
                        await web.upload('https://media.example/input.png', 'image')
                    assert error.value.code == 'media_download_failed'
                    assert 'private download URL' not in str(error.value)
                else:
                    result = await web.upload('https://media.example/input.png', 'image')
                    assert result['file_key'] == 'object'
            finally:
                await web.aclose()
    finally:
        media_context.reset(token)
    assert sum(host == 'media.example' for _, host in calls) == min(failed_gets+1, 3)
    assert sum(host == 'toolkit.artlist.io' for _, host in calls) == (0 if failed_gets == 3 else 2)
    assert sleep.await_count == min(failed_gets, 2)
    assert record['download_attempts'] == min(failed_gets+1, 3)
    assert len([e for e in db.events() if e['kind'] == 'media_download_retry']) == min(failed_gets, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_puts', [1, 3])
async def test_media_upload_retries_only_the_same_presigned_put(setup, failed_puts):
    db, settings, aid = setup
    stored = 'https://artlist-prod-ai-toolkit-custom-user-uploads.s3.eu-central-1.amazonaws.com/object'
    put_url = stored + '?X-Amz-Signature=upload&x-id=PutObject'
    get_url = stored + '?X-Amz-Signature=download&x-id=GetObject'
    calls = []

    def responder(request):
        route = request.url.path.rsplit('/', 1)[-1]
        calls.append((request.method, route, bytes(request.content) if request.method == 'PUT' else b''))
        if request.url.host == 'toolkit.artlist.io':
            value = {'presignedUrl': put_url, 'fileKey': 'object'} if route == 'uploadRouter.getPresignedUrl' else {'presignedUrl': get_url}
            return httpx.Response(200, json={'result': {'data': {'json': value}}})
        if request.url.host == 'media.example':
            return httpx.Response(200, content=b'image-content', headers={'content-type': 'image/png'})
        if request.method == 'PUT':
            if sum(method == 'PUT' for method, _, _ in calls) <= failed_puts:
                raise httpx.RemoteProtocolError('signed URL and private proxy detail', request=request)
            return httpx.Response(200)
        return httpx.Response(206, content=b'x')

    record = {}
    token = media_context.set(record)
    try:
        with patch('app.web.client', factory_for(responder, [])), patch('app.web.probe', AsyncMock(return_value={
            'streams': [{'codec_type': 'video', 'width': 1280, 'height': 720}]
        })), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
            web = WebClient(aid, db, settings)
            try:
                if failed_puts == 3:
                    with pytest.raises(GatewayError) as error:
                        await web.upload('https://media.example/input.png', 'image')
                    assert error.value.code == 'media_upload_failed'
                    assert 'signed URL' not in str(error.value) and 'private proxy detail' not in str(error.value)
                else:
                    result = await web.upload('https://media.example/input.png', 'image')
                    assert result['file_key'] == 'object'
            finally:
                await web.aclose()
    finally:
        media_context.reset(token)
    puts = [body for method, _, body in calls if method == 'PUT']
    assert len(puts) == min(failed_puts + 1, 3)
    assert puts == [b'image-content'] * len(puts)
    assert record['upload_attempts'] == len(puts)
    assert len([1 for method, route, _ in calls if route == 'uploadRouter.getPresignedUrl']) == 1
    assert sleep.await_count == len(puts) - 1
    assert len([e for e in db.events() if e['kind'] == 'media_upload_retry']) == len(puts) - 1


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
    assert proxies == ['socks5://xray:20001']
    assert len(seen) == 2
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
    seen = []
    def responder(request):
        seen.append(request)
        raise httpx.ConnectError('secret-address', request=request)
    with patch('app.web.client', factory_for(responder, proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
        with pytest.raises(GatewayError) as error:
            await WebClient(aid, db, settings).rpc('modelRouter.getCostQuote', {}, post=True)
    assert len(proxies) == 1 and len(seen) == 3
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
async def test_explicit_false_submit_403_allows_one_fresh_preflight(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    proxies = []
    with patch('app.web.client', factory_for(lambda request: httpx.Response(403, json={'success': False}), proxies)):
        with pytest.raises(GatewayError) as error:
            await web.rpc('userGenerationRouter.createUserGeneration', {'test': True}, post=True)
    assert error.value.code == 'generation_rejected' and error.value.retryable
    assert len(proxies) == 1

    service = Service(db, settings)
    web = AsyncMock()
    web.prepare.side_effect = [{'attempt': 1}, {'attempt': 2}]
    web.submit.side_effect = [GatewayError('explicit false 403', 'generation_rejected', 422, retryable=True),
                              'generation-after-refresh']
    web.query.return_value = {'status': 'succeeded', 'video_url': 'https://media.example/result.mp4'}
    service.web = lambda _: web
    task = await service.create({'model': 'sd-2-5-480p', 'prompt': 'test'}, 'explicit-403-once')
    await asyncio.gather(*list(service.jobs.values()))
    assert db.task(task['id'])['status'] == 'succeeded'
    assert web.prepare.await_count == web.submit.await_count == 2
    assert web.submit.await_args_list[0].args == ({'attempt': 1},)
    assert web.submit.await_args_list[1].args == ({'attempt': 2},)


@pytest.mark.asyncio
@pytest.mark.parametrize('failures', [1, 3])
async def test_media_download_failure_retries_only_before_submission(setup, failures):
    db, settings, aid = setup
    service = Service(db, settings)
    web = AsyncMock()
    web.prepare.side_effect = [GatewayError('source connect timeout', 'media_download_failed',
                                            502, retryable=True)] * failures + [{'prepared': True}]
    web.submit.return_value = 'generation-after-download'
    web.query.return_value = {'status': 'succeeded', 'video_url': 'https://media.example/result.mp4'}
    service.web = lambda _: web
    with patch('app.service.asyncio.sleep', new_callable=AsyncMock) as sleep:
        task = await service.create({'model': 'sd-2-5-480p', 'prompt': 'test'}, f'download-{failures}')
        await asyncio.gather(*list(service.jobs.values()))
    stored = db.task(task['id'])
    assert stored['status'] == ('succeeded' if failures == 1 else 'failed')
    assert stored['error_code'] == ('' if failures == 1 else 'media_download_failed')
    assert web.prepare.await_count == (2 if failures == 1 else 3)
    assert web.submit.await_count == (1 if failures == 1 else 0)
    assert len([e for e in db.events() if e['kind'] == 'media_preparation_retry']) == min(failures, 2)
    assert sleep.await_count >= min(failures, 2)


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
@pytest.mark.parametrize('missing_team_id', [True, False])
async def test_bad_request_diagnostics_identify_endpoint_without_echoing_upstream_data(setup, missing_team_id):
    db, settings, aid = setup
    secret = 'private-cookie signed-url-secret'
    message = json.dumps([{'code': 'invalid_type', 'expected': 'string', 'received': 'undefined',
                           'path': ['teamId'], 'message': secret}]) if missing_team_id else secret
    proxies = []
    token = web_task_context.set('task-http400')
    try:
        with patch('app.web.client', factory_for(lambda request: httpx.Response(400, json={'error': {'json': {'message': message}}}), proxies)), patch('app.web.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with pytest.raises(GatewayError) as error:
                await WebClient(aid, db, settings).rpc('chatSession.createChatSession', {'name': 'sample'}, post=True)
        sleep.assert_not_awaited()
    finally:
        web_task_context.reset(token)
    assert len(proxies) == 1
    assert error.value.code == 'generation_rejected'
    assert 'chatSession.createChatSession' in str(error.value)
    assert ('MISSING_SESSION_TEAM_ID' if missing_team_id else 'upstream_bad_request') in str(error.value)
    assert 'private-cookie' not in str(error.value) and 'signed-url-secret' not in str(error.value)
    diagnostic = db.account(aid, True)['credentials']['web_last_rejection']
    assert diagnostic['status'] == 400 and diagnostic['task_id'] == 'task-http400'
    assert diagnostic['procedure'] == 'chatSession.createChatSession'
    assert secret in diagnostic['body']
    assert 'signed-url-secret' not in db.conn.execute('SELECT secret FROM accounts WHERE id=?', (aid,)).fetchone()[0]
    assert 'signed-url-secret' not in json.dumps(db.account(aid)) + json.dumps(db.events())
    assert public_error(error.value.code, str(error.value))['message'] == GENERATION_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_submit_response', [False, True])
async def test_task_recovers_preflight_but_never_repeats_generation(setup, lose_submit_response):
    db, settings, aid = setup
    service = Service(db, settings)
    web = service.web(aid)
    seen, proxies = [], []
    def responder(request):
        procedure = request.url.path.rsplit('/', 1)[-1]
        assert request.headers['x-trpc-source'] == 'nextjs-react'
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
