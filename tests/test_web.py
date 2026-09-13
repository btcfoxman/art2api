import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from app.catalog import normalize_request
from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.service import Service
from app.web import WebClient, unwrap
from app.web_catalog import GROUPS, generation_payload, generation_result, profiles, quote_input, validate_request

MODEL = 'sd-2-5-480p'
CAPTURE = json.loads(Path(__file__).with_name('fixtures').joinpath('web_generation.json').read_text())


@pytest.fixture
def setup(tmp_path):
    settings=Settings(data_dir=tmp_path, api_key='a'*32,admin_token='b'*32,encryption_key=Fernet.generate_key().decode(),poll_interval=1)
    db=Database(tmp_path/'test.db',settings.encryption_key)
    account=db.save_account({'name':'web','proxy_url':'socks5://xray:20001','backend':'web'})
    db.update_credentials(account['id'],{'web_cookie':'session=private-cookie','web_user_agent':'Browser','web_user_id':'user-1'})
    db.update_account(account['id'],profiles=profiles(),status='ready',egress_ip='203.0.113.4')
    db.save_account({'enabled':True},account['id'])
    yield db,settings,account['id']
    db.close()


def test_all_requested_models_have_captured_group_and_exact_resolution():
    mappings=profiles()
    assert len(mappings)==10
    assert mappings['doubao-seedance-2-0-260128-4k']['group_id']==358
    assert mappings['doubao-seedance-2-0-fast-260128']['group_id']==377
    assert mappings['doubao-seedance-2-0-mini-260615']['group_id']==416
    for model,profile in mappings.items():
        request=normalize_request({'model':model,'prompt':'test'})
        validate_request(request,profile)
        assert request['resolution'] in profile['constraints']['resolutions']
    for model in ['sd-2-5-1080p','doubao-seedance-2-0-260128-1080p']:
        assert normalize_request({'model':model,'prompt':'test','resolution':'480p'})['resolution']=='1080p'
    with pytest.raises(ValueError):
        validate_request(normalize_request({'model':'doubao-seedance-2-0-fast-260128','prompt':'test','resolution':'4k'}),mappings['doubao-seedance-2-0-fast-260128'])


def test_multimodal_wire_payload_preserves_order_duration_and_audio_false():
    request=normalize_request({'model':MODEL,'prompt':'@img1 meets @img2','generate_audio':False,'duration':4})
    def asset(name,milliseconds=None):
        return {'file_key':name,'file_url':'https://storage.example/'+name,'metadata':{'byteSize':100,'mimeType':'video/mp4' if milliseconds else 'image/png',**({'durationMs':milliseconds} if milliseconds else {})}}
    assets={'image_urls':[asset('a'),asset('b')],'video_urls':[asset('v1',10000),asset('v2',5088)],'audio_urls':[asset('audio',5146)]}
    quote,inputs,settings,artifacts=quote_input(request,assets)
    assert quote['modelGroupId']==515
    assert settings['generate_audio'] is False
    assert settings['user_inputs_metadata']['video_urls']==[{'duration':10.0},{'duration':5.088}]
    assert [t['tagId'] for t in inputs['tagReferences']]==['@img1','@img2','@vid1','@vid2','@aud1']
    assert [a['fileKey'] for a in artifacts]==['a','b','v1','v2','audio']
    resolved={'modelId':3011,'modelFeature':'multi-to-video','cost':800,'digitalSignature':'fresh-signature','timestamp':123}
    payload=generation_payload('session-1',resolved,inputs,settings,artifacts,'operator-verification')
    assert payload['modelGroupId']==3011 and payload['costQuoteDigitalSignature']=='fresh-signature'
    assert payload['inputs']['video_urls'][1]['fileUrl'].endswith('v2')


def test_start_end_frames_are_not_silently_merged_into_references():
    request=normalize_request({'model':'doubao-seedance-2-0-mini-260615','prompt':'test','first_frame':'https://media.example/a.png','last_frame':'https://media.example/b.png'})
    assets={k:[{'file_key':k,'file_url':url,'metadata':{}}] for k,url in [('first_frame',request['first_frame']),('last_frame',request['last_frame'])]}
    quote,inputs,settings,artifacts=quote_input(request,assets)
    assert settings['generation_mode']=='endFrame'
    assert inputs['image_url']==request['first_frame']
    assert inputs['end_frame']==request['last_frame']
    with pytest.raises(ValueError):
        validate_request({**request,'image_urls':['https://media.example/extra.png']},profiles()[request['model']])


def test_verified_task_tokens_are_encrypted_bound_and_single_use(setup):
    db,_,aid=setup
    req=normalize_request({'model':MODEL,'prompt':'test'})
    token='normal-user-verification-'+'x'*40
    db.save_verification(aid,token)
    first,_=db.create_task(req,[(aid,profiles()[MODEL])],'first',10)
    again,created=db.create_task(req,[],'first',10)
    assert not created and first['id']==again['id']
    assert token not in json.dumps(db.account(aid)) and token not in db.conn.execute('SELECT secret FROM web_verifications').fetchone()[0]
    assert db.take_verification(first['id'])==token
    with pytest.raises(GatewayError):db.take_verification(first['id'])
    with pytest.raises(ValueError):db.save_verification(aid,token)


def test_captured_completed_response_and_pending_output_are_distinct():
    result=generation_result(CAPTURE)
    assert result['status']=='succeeded' and result['output_id']=='output-1'
    assert generation_result({'status':'Completed','outputs':[]})['status']=='running'
    assert generation_result({'status':'InProgress'})['status']=='running'
    assert generation_result({'status':'Failed'})['status']=='failed'
    with pytest.raises(GatewayError) as error: generation_result({'status':'something-new'})
    assert error.value.retryable


@pytest.mark.asyncio
async def test_query_uses_generation_then_output_id_and_rejects_mismatch(setup):
    db,settings,aid=setup
    web=WebClient(aid,db,settings)
    web.rpc=AsyncMock(side_effect=[CAPTURE,{'id':'output-1','generationId':'generation-1','fileUrl':'https://media.example/result.mp4'}])
    assert (await web.query('generation-1'))['video_url'].endswith('result.mp4')
    assert web.rpc.await_args_list[0].args[1]=={'id':'generation-1'}
    assert web.rpc.await_args_list[1].args[1]=={'id':'output-1'}
    web.rpc=AsyncMock(side_effect=[CAPTURE,{'id':'output-1','generationId':'another-account-task','fileUrl':'https://media.example/result.mp4'}])
    with pytest.raises(GatewayError):await web.query('generation-1')


@pytest.mark.asyncio
async def test_web_submit_interrupt_never_falls_back_or_resubmits(setup):
    db,settings,aid=setup
    db.save_verification(aid,'verified-once-'+'z'*40)
    service=Service(db,settings)
    web=AsyncMock()
    web.prepare.return_value={'prepared':True}
    web.submit.side_effect=GatewayError('connection interrupted','proxy_error',retryable=True)
    service.web=lambda _:web
    task=await service.create({'model':MODEL,'prompt':'test'},'stable-key')
    await asyncio.gather(*list(service.jobs.values()))
    assert db.task(task['id'])['status']=='submission_unknown'
    await service.create({'model':MODEL,'prompt':'test'},'stable-key')
    assert web.submit.await_count==1


@pytest.mark.asyncio
async def test_web_restart_queries_original_id_without_token_or_new_submit(setup):
    db,settings,aid=setup
    db.save_verification(aid,'verified-once-'+'z'*40)
    req=normalize_request({'model':MODEL,'prompt':'test'})
    task,_=db.create_task(req,[(aid,profiles()[MODEL])],'restart',10)
    db.take_verification(task['id'])
    db.update_task(task['id'],status='running',upstream_id='generation-1')
    service=Service(db,settings)
    web=AsyncMock()
    web.query.return_value={'status':'succeeded','video_url':'https://media.example/result.mp4','output_id':'output-1'}
    service.web=lambda _:web
    await service.run(task['id'])
    assert db.task(task['id'])['status']=='succeeded'
    web.query.assert_awaited_once_with('generation-1')
    web.prepare.assert_not_awaited()
    web.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_web_protocol_preserves_proxy_and_does_not_log_session(setup):
    db,settings,aid=setup
    seen=[]
    def responder(request):
        seen.append(request)
        return httpx.Response(200,json={'result':{'data':{'json':{'success':True,'data':{'id':'value'}}}}},headers={'set-cookie':'session=rotated-private; Path=/; Secure; HttpOnly'})
    def factory(proxy,timeout,**kwargs):
        assert proxy=='socks5://xray:20001'
        return httpx.AsyncClient(transport=httpx.MockTransport(responder),**kwargs)
    with patch('app.web.client',factory):
        web=WebClient(aid,db,settings)
        assert await web.rpc('userGenerationRouter.getUserGenerationById',{'id':'generation-1'})=={'id':'value'}
    assert json.loads(seen[0].url.params['input'])=={'json':{'id':'generation-1'}}
    assert seen[0].headers['cookie']=='session=private-cookie'
    assert 'rotated-private' not in json.dumps(db.account(aid))
    assert db.account(aid,True)['credentials']['web_cookie']=='session=rotated-private'


@pytest.mark.asyncio
async def test_cookie_rotation_keeps_login_after_json_consent_cookie(setup):
    db,settings,aid=setup
    cookie='CookieScriptConsent={"action":"accept"}; auth=private-login'
    db.update_credentials(aid,{'web_cookie':cookie})
    def factory(*args,**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'user':{}},headers={'set-cookie':'rotation=next; Path=/; Secure'})),**kwargs)
    with patch('app.web.client',factory):
        await WebClient(aid,db,settings).request('GET','/api/auth/session')
    assert db.account(aid,True)['credentials']['web_cookie']==cookie+'; rotation=next'


def test_trpc_explicit_rejection_is_not_a_generation_id():
    for body in [{'success':False},{'result':{'data':{'json':{'success':False}}}},{'error':{'message':'rejected'}}]:
        with pytest.raises(GatewayError) as exc:unwrap(body)
        assert exc.value.code=='generation_rejected'


@pytest.mark.asyncio
@pytest.mark.parametrize('with_verification', ['background_token', 'background_error', 'provided'])
async def test_compatible_api_submits_signed_web_task_and_returns_video(tmp_path, with_verification):
    from app.main import create_app
    settings=Settings(data_dir=tmp_path,api_key='a'*32,admin_token='b'*32,encryption_key=Fernet.generate_key().decode())
    app=create_app(settings)
    db,service=app.state.db,app.state.service
    account=db.save_account({'name':'web','proxy_url':'socks5://xray:20001','backend':'web'})
    aid=account['id']
    db.update_credentials(aid,{'web_cookie':'private-session','web_user_agent':'Browser'})
    db.update_account(aid,profiles=profiles(),status='ready')
    db.save_account({'enabled':True},aid)
    if with_verification == 'provided':
        db.save_verification(aid,'normal-unused-verification-'+'x'*40)
    service.browsers.generation_verification = AsyncMock(return_value={'client_error':'600010'} if with_verification == 'background_error' else {'token':'background-normal-token'})
    web=service.web(aid)
    quote={'modelId':3009,'cost':1000,'modelFeature':'text-to-video','digitalSignature':'current-signature','timestamp':123,'modelContextConfig':{}}
    responses={
        'modelRouter.getCostQuote':quote,
        'modelRouter.getModel':{'modelGroupId':515,'configs':[{'internalConfig':{'properties':{'input':{'type':'object','required':['prompt','duration','resolution']}}}}]},
        'userGenerationRouter.checkGenerationEligibility':{'isFairUseExceeded':False,'isConcurrencyExceeded':False},
        'chatSession.createChatSession':{'id':'session-1'},
        'userGenerationRouter.createUserGeneration':{'id':'generation-1'},
        'userGenerationRouter.getUserGenerationById':CAPTURE,
        'userGenerationRouter.getUserGenerationOutputById':{'id':'output-1','generationId':'generation-1','fileUrl':'https://media.example/result.mp4'},
    }
    def rpc_response(name,*args,**kwargs):
        if name == 'modelRouter.getCostQuote' and with_verification != 'provided':
            service.browsers.generation_verification.assert_awaited_once_with(aid)
        return responses[name]
    web.rpc=AsyncMock(side_effect=rpc_response)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost:8797',headers={'Authorization':'Bearer '+settings.api_key,'Idempotency-Key':'lingya2api:task-1'}) as api:
            response=await api.post('/api/v3/contents/generations/tasks',json={'model':MODEL,'prompt':'test','duration':5})
            assert response.status_code==200
            task_id=response.json()['id']
            await asyncio.gather(*list(service.jobs.values()))
            result=(await api.get('/api/v3/contents/generations/tasks/'+task_id)).json()
            assert result['status']=='succeeded'
            assert result['content']['video_url']=='https://media.example/result.mp4'
            submitted=next(c.args[1] for c in web.rpc.await_args_list if c.args[0]=='userGenerationRouter.createUserGeneration')
            assert submitted['modelGroupId']==3009 and submitted['costQuoteDigitalSignature']=='current-signature'
            if with_verification == 'provided':
                assert submitted['turnstileToken'].startswith('normal-unused-verification-')
                service.browsers.generation_verification.assert_not_awaited()
            elif with_verification == 'background_token':
                assert submitted['turnstileToken'] == 'background-normal-token'
                service.browsers.generation_verification.assert_awaited_once_with(aid)
            else:
                assert 'turnstileToken' not in submitted
                assert submitted['turnstileClientError'] == '600010'
                service.browsers.generation_verification.assert_awaited_once_with(aid)
            repeated=await api.post('/api/v3/contents/generations/tasks',json={'model':MODEL,'prompt':'test','duration':5})
            assert repeated.json()['id']==task_id
            assert sum(c.args[0]=='userGenerationRouter.createUserGeneration' for c in web.rpc.await_args_list)==1
            assert 'normal-unused-verification' not in json.dumps(db.task(task_id))
    finally:
        await service.stop()
        db.close()


@pytest.mark.asyncio
async def test_forbidden_diagnostics_never_expose_arbitrary_upstream_data(setup):
    db, settings, aid = setup
    def factory(*args, **kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={'error':{'json':{'message':'TURNSTILE_VERIFICATION_FAILED private-cookie secret-signature'}}})), **kwargs)
    with patch('app.web.client',factory):
        with pytest.raises(GatewayError) as error:
            await WebClient(aid,db,settings).submit({})
    assert error.value.code == 'generation_rejected'
    assert 'TURNSTILE_VERIFICATION_FAILED' in str(error.value)
    assert 'private-cookie' not in str(error.value) and 'secret-signature' not in str(error.value)


@pytest.mark.asyncio
async def test_background_verification_failure_never_enters_submit(setup):
    db, settings, aid = setup
    service = Service(db, settings)
    web = AsyncMock()
    web.prepare.side_effect = GatewayError('Normal verification needs operator', 'verification_required', 503)
    service.web = lambda _: web
    task = await service.create({'model':MODEL,'prompt':'test'}, 'verification-failed')
    await asyncio.gather(*list(service.jobs.values()))
    assert db.task(task['id'])['status'] == 'failed'
    assert db.task(task['id'])['error_code'] == 'verification_required'
    web.submit.assert_not_awaited()
