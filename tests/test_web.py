import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.fernet import Fernet

from app.catalog import normalize_request
from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.service import Service
from app.web import WebClient, unwrap
from app.web_catalog import GROUPS, generation_payload, generation_result, profiles, quote_input, reference_prompt, validate_request

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
    request=normalize_request({'model':MODEL,'prompt':'@img1 meets @img2; motion @vid1 @vid2, sound @aud1','generate_audio':False,'duration':4,
                               'image_urls':['https://media.example/a','https://media.example/b'],
                               'video_urls':['https://media.example/v1','https://media.example/v2'],'audio_urls':['https://media.example/a1']})
    def asset(name,milliseconds=None):
        return {'file_key':name,'file_url':'https://storage.example/'+name,'metadata':{'byteSize':100,'mimeType':'video/mp4' if milliseconds else 'image/png',**({'durationMs':milliseconds} if milliseconds else {})}}
    assets={'image_urls':[asset('a'),asset('b')],'video_urls':[asset('v1',10000),asset('v2',5088)],'audio_urls':[asset('audio',5146)]}
    quote,inputs,settings,artifacts=quote_input(request,assets)
    assert quote['modelGroupId']==515
    assert quote['input']['aspect_ratio']==settings['aspect_ratio']=='auto'
    assert settings['generate_audio'] is False
    assert settings['user_inputs_metadata']['video_urls']==[{'duration':10.0},{'duration':5.088}]
    assert [t['tagId'] for t in inputs['tagReferences']]==['@img1','@img2','@vid1','@vid2','@aud1']
    assert [a['fileKey'] for a in artifacts]==['a','b','v1','v2','audio']
    resolved={'modelId':3011,'modelFeature':'multi-to-video','cost':800,'digitalSignature':'fresh-signature','timestamp':123}
    payload=generation_payload('session-1',resolved,inputs,settings,artifacts,'operator-verification')
    assert payload['modelGroupId']==3011 and payload['costQuoteDigitalSignature']=='fresh-signature'
    assert payload['inputs']['video_urls'][1]['fileUrl'].endswith('v2')


def test_reference_aliases_match_prompt_and_keep_original_asset_indices():
    request=normalize_request({'model':MODEL,'prompt':'保留@视频1 的动作，使用@参考2 的形象和@参考1 的服装，声音@音频1。再次@参考2。',
                               'image_urls':['https://media.example/first','https://media.example/second'],
                               'video_urls':['https://media.example/motion'],'audio_urls':['https://media.example/sound']})
    assets={field:[{'file_key':str(i),'file_url':url,'metadata':{'durationMs':4000}} for i,url in enumerate(request[field])]
            for field in ['image_urls','video_urls','audio_urls']}
    quote,inputs,settings,_=quote_input(request,assets)
    expected='保留@vid1 的动作，使用@img2 的形象和@img1 的服装，声音@aud1。再次@img2。'
    assert quote['input']['prompt']==inputs['prompt']==settings['prompt']==expected
    assert inputs['tagReferences']==settings['tagReferences']==[
        {'tagId':'@img1','type':'@img','orderForType':1}, {'tagId':'@img2','type':'@img','orderForType':2},
        {'tagId':'@vid1','type':'@vid','orderForType':1}, {'tagId':'@aud1','type':'@aud','orderForType':1}]
    assert [v['fileUrl'] for v in inputs['image_urls']]==request['image_urls']
    partial={**request,'prompt':'仅使用@image2，重复@IMG2，普通文字。'}
    assert reference_prompt(partial)==('仅使用@img2，重复@img2，普通文字。',[{'tagId':'@img2','type':'@img','orderForType':2}])


def test_media_without_prompt_mentions_does_not_invent_reference_tags():
    request=normalize_request({'model':MODEL,'prompt':'根据图片生成视频，保持原图风格。','image_urls':['https://media.example/image']})
    quote,inputs,settings,artifacts=quote_input(request,{'image_urls':[{'file_key':'image','file_url':request['image_urls'][0],'metadata':{}}]})
    assert 'tagReferences' not in inputs and 'tagReferences' not in settings and 'tagReferences' not in quote['input']
    assert len(inputs['image_urls'])==len(artifacts)==1


@pytest.mark.parametrize('prompt',['@参考2','@img0','@视频1','@audio1'])
def test_missing_prompt_reference_is_rejected_before_upload(prompt):
    request=normalize_request({'model':MODEL,'prompt':prompt,'image_urls':['https://media.example/one']})
    with pytest.raises(ValueError,match='没有对应素材'):
        validate_request(request,profiles()[MODEL])


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
@pytest.mark.parametrize('kind,mime,extension', [
    ('image','image/png','.png'), ('video','video/mp4','.mp4'), ('audio','audio/mpeg','.mp3'),
    ('audio','audio/wav','.wav'), ('audio','audio/x-wav','.wav'), ('audio','Audio/WAV; charset=binary','.wav'),
])
async def test_uploaded_media_uses_separate_get_signature_and_checks_readability(setup, kind, mime, extension):
    db,settings,aid=setup
    source='https://media.example/reference'
    stored='https://artlist-prod-ai-toolkit-custom-user-uploads.s3.eu-central-1.amazonaws.com/object'
    write_url=stored+'?X-Amz-Signature=private-upload&x-id=PutObject'
    read_url=stored+'?X-Amz-Signature=private-download&x-id=GetObject'
    seen=[]
    def responder(request):
        seen.append(request)
        if request.url.host=='toolkit.artlist.io':
            name=request.url.path.rsplit('/',1)[-1]
            payload=json.loads(request.content)['json']
            if name=='uploadRouter.getPresignedUrl':
                assert payload['fileName'].endswith(extension)
                data={'presignedUrl':write_url,'fileUrl':stored,'fileKey':'object'}
            else:
                assert name=='uploadRouter.getPresignedUrlFromKey'
                assert payload=={'fileKey':'object','expiresIn':86400}
                assert any(r.method=='PUT' for r in seen)
                data={'presignedUrl':read_url}
            return httpx.Response(200,json={'result':{'data':{'json':data}}})
        assert 'cookie' not in request.headers and 'authorization' not in request.headers
        if str(request.url)==source:
            return httpx.Response(200,content=b'media',headers={'content-type':mime})
        if request.method=='PUT':
            assert str(request.url)==write_url and request.content==b'media'
            return httpx.Response(200)
        assert str(request.url)==read_url and request.method=='GET'
        assert request.headers['range']=='bytes=0-0'
        return httpx.Response(206,content=b'm')
    def factory(proxy,*args,**kwargs):
        assert proxy=='socks5://xray:20001'
        return httpx.AsyncClient(transport=httpx.MockTransport(responder),**kwargs)
    probe=AsyncMock()
    probe.returncode=0
    probe.communicate.return_value=(json.dumps({'streams':[{'codec_type':'video','width':1280,'height':720,'avg_frame_rate':'24/1'}],
                                               'format':{'duration':'4.0'}}).encode(),b'')
    # Reproduce the Docker MIME database that does not register audio/wav.
    with patch('app.web.client',factory),patch('app.web.asyncio.create_subprocess_exec',AsyncMock(return_value=probe)),patch('app.web.mimetypes.guess_extension',return_value=None):
        asset=await WebClient(aid,db,settings).upload(source,kind)
    assert asset['file_url']==read_url
    assert asset['metadata']['fileName'].endswith(extension)
    if kind=='audio':
        WebClient.validate_media({'audio_urls':[asset]}, {'audioFormats':['WAV','MP3'],'minUploadedAudioDuration':4})
    request=normalize_request({'model':MODEL,'prompt':'reference test','duration':4})
    _,inputs,settings,artifacts=quote_input(request,{kind+'_urls':[asset]})
    assert inputs[kind+'_urls'][0]['fileUrl']==read_url
    assert artifacts[0]['metadata']['fileUrl']==read_url
    assert parse_qs(read_url.split('?',1)[1])['x-id']==['GetObject']


def test_valid_wav_extension_does_not_bypass_duration_or_format_limits():
    asset={'metadata':{'fileName':'reference.wav','mimeType':'audio/wav','byteSize':123558,'durationMs':2800}}
    context={'audioFormats':['WAV','MP3'],'minUploadedAudioDuration':4}
    with pytest.raises(ValueError,match='音频 1 为 2.8 秒，要求至少 4 秒'):
        WebClient.validate_media({'audio_urls':[asset]},context)
    asset['metadata']['durationMs']=4000
    WebClient.validate_media({'audio_urls':[asset]},context)
    asset['metadata']['fileName']='unsupported.aac'
    with pytest.raises(ValueError,match='音频 1 为 AAC；允许 WAV、MP3'):
        WebClient.validate_media({'audio_urls':[asset]},context)


@pytest.mark.parametrize('fps', [24, 24000/1001, 60, 17424000/290381])
def test_nominal_boundary_fps_accepts_container_rounding(fps):
    metadata={'fileName':'reference.mp4','byteSize':100,'durationMs':10083,'fps':fps}
    WebClient.validate_media({'video_urls':[{'metadata':metadata}]}, {'minVideoFps':24,'maxVideoFps':60})


@pytest.mark.parametrize('fps', [23, 23.9, 60.1, 61, 120])
def test_fps_tolerance_does_not_accept_out_of_range_video(fps):
    metadata={'fileName':'reference.mp4','byteSize':100,'durationMs':10083,'fps':fps}
    with pytest.raises(ValueError,match='参考视频帧率不受支持：视频 1'):
        WebClient.validate_media({'video_urls':[{'metadata':metadata}]}, {'minVideoFps':24,'maxVideoFps':60})


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unreadable', 'unsigned', 'wrong_object', 'put_signature'])
async def test_invalid_uploaded_media_never_reaches_generation(setup, failure):
    db,settings,aid=setup
    stored='https://artlist-prod-ai-toolkit-custom-user-uploads.s3.eu-central-1.amazonaws.com/object'
    preview=stored+'?X-Amz-Signature=download&x-id=GetObject'
    preview={'unsigned':stored,'wrong_object':preview.replace('/object?','/other?'),
             'put_signature':preview.replace('GetObject','PutObject')}.get(failure,preview)
    called=[]
    def responder(request):
        if request.url.host=='toolkit.artlist.io':
            name=request.url.path.rsplit('/',1)[-1];called.append(name)
            if name=='uploadRouter.getPresignedUrl':
                data={'presignedUrl':stored+'?x-id=PutObject','fileKey':'object','fileUrl':stored}
            else:
                assert name=='uploadRouter.getPresignedUrlFromKey'
                data={'presignedUrl':preview}
            return httpx.Response(200,json={'result':{'data':{'json':data}}})
        if request.url.host=='media.example':
            return httpx.Response(200,content=b'image',headers={'content-type':'image/png'})
        return httpx.Response(200 if request.method=='PUT' else 403)
    def factory(*args,**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(responder),**kwargs)
    probe=AsyncMock();probe.returncode=0
    probe.communicate.return_value=(b'{"streams":[{"codec_type":"video","width":1280,"height":720}]}',b'')
    service=Service(db,settings)
    service.browsers.generation_verification=AsyncMock()
    with patch('app.web.client',factory),patch('app.web.asyncio.create_subprocess_exec',AsyncMock(return_value=probe)):
        task=await service.create({'model':MODEL,'prompt':'test','image_urls':['https://media.example/image.png']},'bad-media')
        await asyncio.gather(*list(service.jobs.values()))
    task=db.task(task['id'])
    assert task['status']=='failed' and not task['upstream_id']
    assert task['error_code']==('media_unreachable' if failure=='unreadable' else 'media_signature_invalid')
    assert called==['uploadRouter.getPresignedUrl','uploadRouter.getPresignedUrlFromKey']
    service.browsers.generation_verification.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_failure_preserves_safe_upstream_code_without_signed_url(setup):
    db,settings,aid=setup
    data={'status':'Failed','errorCode':'INPUT_URL_UNREACHABLE',
          'reason':'Input URL unreachable (HTTP 403): https://storage.example/image?X-Amz-Signature=private-signature'}
    result=generation_result(data)
    assert result['upstream_error_code']=='INPUT_URL_UNREACHABLE'
    assert 'INPUT_URL_UNREACHABLE' in result['error_message']
    assert 'private-signature' not in json.dumps(result) and 'storage.example' not in json.dumps(result)
    task,_=db.create_task(normalize_request({'model':MODEL,'prompt':'test'}),[(aid,profiles()[MODEL])],'failed-generation',10)
    db.update_task(task['id'],status='running',upstream_id='original-generation',result={'resolved_model_id':3011})
    service=Service(db,settings);web=AsyncMock();web.query.return_value=result
    service.web=lambda _:web
    await service.run(task['id'])
    stored=db.task(task['id'])
    assert stored['result']['resolved_model_id']==3011
    assert stored['result']['upstream_error_code']=='INPUT_URL_UNREACHABLE'
    assert 'INPUT_URL_UNREACHABLE' in service.public_task(stored)['error']['message']
    web.submit.assert_not_awaited()
    unsafe=generation_result({'status':'Failed','errorCode':'private-token\nURL https://private.example'})
    assert unsafe['upstream_error_code']=='' and 'private-token' not in unsafe['error_message']
    review=generation_result({'status':'Failed','errorCode':'PROVIDER_CONTENT_SAFETY_VIOLATION',
                              'reason':'OutputAudioSensitiveContentDetected.PolicyViolation: copyright restrictions https://private.example/?signature=secret'})
    assert review['error_code']=='generation_failed' and '音频' in review['error_message'] and '版权限制' in review['error_message']
    assert 'private.example' not in json.dumps(review) and 'signature' not in json.dumps(review)
    video_review=generation_result({'status':'Failed','errorCode':'PROVIDER_CONTENT_SAFETY_VIOLATION',
                                    'reason':'OutputVideoSensitiveContentDetected.PolicyViolation: copyright restrictions https://private.example/?signature=secret'})
    assert '输出视频' in video_review['error_message'] and '版权限制' in video_review['error_message']
    assert 'private.example' not in json.dumps(video_review) and 'signature' not in json.dumps(video_review)
    generic=generation_result({'status':'Failed','errorCode':'PROVIDER_CONTENT_SAFETY_VIOLATION','reason':'other policy violation'})
    assert '内容审核未通过' in generic['error_message'] and '版权' not in generic['error_message']
    audio=generation_result({'status':'Failed','errorCode':'FILE_OPTIMIZATION_FAILED',
                             'reason':'Asset processing returned Failed: the input audio may contain sensitive information https://private.example'})
    assert '参考音频审核未通过' in audio['error_message'] and 'private.example' not in str(audio)
    optimization=generation_result({'status':'Failed','errorCode':'FILE_OPTIMIZATION_FAILED','reason':'other failure'})
    assert '预处理失败' in optimization['error_message'] and '审核' not in optimization['error_message']


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
