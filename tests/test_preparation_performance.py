import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.catalog import normalize_request
from app.errors import GatewayError
from app.service import Service
from app.web import WebClient, media_context, preparation_context
from app.web_catalog import profiles
from test_web_transport import setup, factory_for


MODEL = 'doubao-seedance-2-0-fast-260128'


@pytest.mark.asyncio
async def test_pools_reuse_connections_use_current_session_and_isolate_media(setup):
    db, settings, aid = setup
    service = Service(db, settings)
    web = service.web(aid)
    clients, proxies, seen = [], [], []

    def responder(request):
        seen.append(request)
        if request.url.host == 'toolkit.artlist.io':
            if len(seen) == 1:
                # Import completes while an older HTTP request is in flight.
                db.update_credentials(aid, {'web_cookie': 'session=new-import', 'web_user_agent': 'new-agent'})
            return httpx.Response(200, json={}, headers={'set-cookie': 'session=old-response; Path=/'})
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        return httpx.Response(200, headers={'set-cookie': 'tracking=never-send; Path=/'})

    def factory(proxy, timeout, **kwargs):
        proxies.append(proxy)
        http = httpx.AsyncClient(transport=httpx.MockTransport(responder), **kwargs)
        clients.append(http)
        return http

    with patch('app.web.client', factory):
        await web.request('GET', '/api/auth/session')
        assert db.account(aid, True)['credentials']['web_cookie'] == 'session=new-import'
        settings.request_timeout = 91
        await web.request('GET', '/api/auth/session')
        async with web.media_client() as http:
            await http.get('https://media.example/a.png')
        async with web.media_client() as http:
            await http.get('https://media.example/b.png')
        assert len(clients) == 2 and not any(c.cookies for c in clients)
        assert seen[1].headers['cookie'] == 'session=new-import'
        assert seen[1].headers['user-agent'] == 'new-agent'
        assert seen[1].extensions['timeout']['read'] == 91
        assert seen[0].headers['x-request-id'] != seen[1].headers['x-request-id']
        assert proxies == ['socks5://xray:20001'] * 2
        await service.stop()
        assert all(c.is_closed for c in clients)


@pytest.mark.asyncio
async def test_changed_proxy_never_reuses_old_pool(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    proxies = []
    with patch('app.web.client', factory_for(lambda r: httpx.Response(200, json={}), proxies)):
        await web.request('GET', '/api/auth/session')
        db.save_account({'proxy_url': 'socks5://xray:20002'}, aid)
        for call in (web.request('GET', '/api/auth/session'), web.upload('https://media.example/a.png', 'image')):
            with pytest.raises(GatewayError, match='固定代理已变更'):
                await call
        await web.aclose()
    assert proxies == ['socks5://xray:20001']


@pytest.mark.asyncio
async def test_parallel_assets_preserve_reference_order_and_share_account_limit(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    db.save_account({'max_concurrency': 5}, aid)
    request = normalize_request({'model': MODEL, 'prompt': 'test',
                                 'image_urls': [f'https://media.example/{i}.png' for i in range(4)]})
    tasks = [db.create_task(request, [(aid, profiles()[MODEL])], str(i), 100)[0] for i in range(5)]
    active, peak = 0, 0
    full_pool = asyncio.Event()
    per_task, task_peak = {}, {}

    async def upload(url, kind, record, **options):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        task_id = asyncio.current_task().get_name().split(':')[0]
        per_task[task_id] = per_task.get(task_id, 0) + 1
        task_peak[task_id] = max(task_peak.get(task_id, 0), per_task[task_id])
        if active == 12:
            full_pool.set()
        try:
            await asyncio.wait_for(full_pool.wait(), 1)
            # Intentionally finish in a different order from the request.
            await asyncio.sleep((4-int(url.rsplit('/', 1)[-1][0]))*.005)
            return {'file_url': url, 'metadata': {}}
        finally:
            active -= 1
            per_task[task_id] -= 1

    web._upload = upload
    timings = [{} for _ in tasks]
    # Tag child coroutines with their logical request to count per-task slots.
    original_upload = web.upload
    async def tagged_upload(*args, **kwargs):
        timing = preparation_context.get()
        asyncio.current_task().set_name(str(id(timing))+':media')
        return await original_upload(*args, **kwargs)
    web.upload = tagged_upload
    async def prepare(task, timing):
        context = preparation_context.set(timing)
        try:
            return await web._prepare_media(task, timing)
        finally:
            preparation_context.reset(context)
    assets = await asyncio.gather(*(prepare(task, timing) for task, timing in zip(tasks, timings)))
    assert peak == 12 and active == 0
    assert all(value <= 3 for value in task_peak.values())
    for result, timing in zip(assets, timings):
        assert [a['file_url'] for a in result['image_urls']] == request['image_urls']
        assert [r['index'] for r in timing['media_items']] == [1, 2, 3, 4]
        assert all(r['total_seconds'] >= r['queue_seconds'] >= 0 for r in timing['media_items'])
        assert 'media.example' not in json.dumps(timing)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_parent', [False, True])
async def test_media_failure_or_cancellation_drains_siblings_and_releases_slots(setup, cancel_parent):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    started = asyncio.Event()
    active, completed = 0, 0

    async def upload(url, kind, record, **options):
        nonlocal active, completed
        active += 1
        if active == 3:
            started.set()
        try:
            await started.wait()
            if url.endswith('/0') and not cancel_parent:
                raise ValueError('reference failed')
            await asyncio.Event().wait()
        finally:
            active -= 1
            completed += 1

    web._upload = upload
    parent = asyncio.create_task(web.parallel_uploads([web.upload(f'https://media.example/{i}', 'image') for i in range(15)]))
    await asyncio.wait_for(started.wait(), 1)
    if cancel_parent:
        parent.cancel()
    with pytest.raises(asyncio.CancelledError if cancel_parent else ValueError):
        await asyncio.wait_for(parent, 1)
    assert active == 0 and completed >= 3
    for _ in range(12):
        await asyncio.wait_for(web.media_slots.acquire(), .2)
    assert media_context.get() is None


@pytest.mark.asyncio
async def test_model_schema_cache_expires_and_refreshes_invalid_cached_definition(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    request = normalize_request({'model': MODEL, 'prompt': 'test'})
    group = profiles()[MODEL]['group_id']
    model = {'modelGroupId': group, 'configs': [{'internalConfig': {'properties': {'input': {'type': 'object'}}}}]}
    quotes, definitions = [], []

    async def rpc(name, value, **kwargs):
        if name == 'modelRouter.getCostQuote':
            quotes.append(value)
            return {'modelId': 100, 'cost': 1, 'modelFeature': 'text-to-video',
                    'digitalSignature': str(len(quotes)), 'timestamp': len(quotes)}
        assert name == 'modelRouter.getModel'
        definitions.append(value)
        return model

    web.rpc = rpc
    first = await web.quote(request)
    second = await web.quote(request)
    assert len(quotes) == 2 and len(definitions) == 1
    assert first[0]['digitalSignature'] != second[0]['digitalSignature']
    key = ('user-1', 100)
    timestamp, _ = web.model_cache[key]
    web.model_cache[key] = (timestamp, {'modelGroupId': -1})
    await web.quote(request)
    assert len(definitions) == 2  # Stale schema cannot reject a valid request.
    web.model_cache[key] = (timestamp-301, model)
    await web.quote(request)
    assert len(definitions) == 3
    db.update_credentials(aid, {'web_user_id': 'user-2'})
    await web.quote(request)
    assert len(definitions) == 4


@pytest.mark.asyncio
async def test_rpc_timing_reports_lock_wait_and_request_time_separately(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    record = {}
    context = preparation_context.set(record)
    await web.lock.acquire()
    async def respond(request):
        await asyncio.sleep(.01)
        return httpx.Response(200, json={})
    try:
        with patch('app.web.client', factory_for(respond, [])):
            job = asyncio.create_task(web.request('GET', '/api/auth/session'))
            await asyncio.sleep(.02)
            web.lock.release()
            await job
            await web.aclose()
    finally:
        preparation_context.reset(context)
    metric = record['requests']['session']
    assert metric['calls'] == 1 and metric['queue_seconds'] >= .01 and metric['request_seconds'] >= .005
    assert 'private-cookie' not in json.dumps(record)


@pytest.mark.asyncio
async def test_parent_cancellation_waits_for_media_cleanup_without_recancelling(setup):
    db, settings, aid = setup
    web = WebClient(aid, db, settings)
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cleaned = []
    async def quick():
        await asyncio.Event().wait()
    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()  # Stand-in for FFmpeg wait or pending disk I/O.
            cleaned.append(True)
    parent = asyncio.create_task(web.parallel_uploads([quick(), slow()]))
    await started.wait()
    parent.cancel()
    await cleaning.wait()
    try:
        await asyncio.sleep(.01)
        assert not parent.done(), 'Cancellation interrupted media cleanup'
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await parent
    assert cleaned == [True]
