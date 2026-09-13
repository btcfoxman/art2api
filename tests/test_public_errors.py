import json
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.catalog import normalize_request
from app.config import Settings
from app.errors import GatewayError
from app.main import create_app
from app.public_errors import (
    PUBLIC_MESSAGES, public_message, QUEUE_LIMIT, QUEUE_INTERRUPTED, CONTENT_POLICY,
    OUTPUT_VIDEO_POLICY, INPUT_PERSON, REFERENCE_PERSON, TEXT_POLICY, IMAGE_POLICY,
    VIDEO_POLICY, MEDIA_DURATION, MEDIA_LIMIT, MEDIA_FORMAT, MEDIA_DOWNLOAD,
    MEDIA_EXTERNAL, GENERATION_FAILED,
)
from app.web_catalog import generation_result, profiles

MODEL = 'doubao-seedance-2-0-260128'
PATHS = ['/v1/videos', '/api/v3/contents/generations/tasks']
PRIVATE = 'Artlist ARTAPI private-token https://private.example/?signature=secret'


@pytest.fixture
def api(tmp_path):
    settings = Settings(data_dir=tmp_path, api_key='a'*32, admin_token='b'*32,
                        encryption_key=Fernet.generate_key().decode())
    app = create_app(settings)
    app.state.service.schedule = Mock()
    with TestClient(app) as http:
        yield app, http, {'Authorization': 'Bearer '+settings.api_key}


def assert_public(response, status, message=None):
    assert response.status_code == status
    payload = response.json()
    assert payload['error']['message'] in PUBLIC_MESSAGES
    if message:
        assert payload['error']['message'] == message
    text = json.dumps(payload).lower()
    for forbidden in ('artlist', 'artapi', 'private-token', 'private.example', 'signature', 'traceback'):
        assert forbidden not in text
    assert 'detail' not in payload
    return payload


@pytest.mark.parametrize(('code', 'diagnostic', 'upstream', 'expected'), [
    ('generation_failed', 'Artlist 拒绝参考视频：可能涉及版权限制', 'FILE_OPTIMIZATION_FAILED', VIDEO_POLICY),
    ('generation_failed', 'Artlist 参考音频审核未通过', 'FILE_OPTIMIZATION_FAILED', CONTENT_POLICY),
    ('generation_failed', 'Transaction failed', 'INSUFFICIENT_CREDITS', QUEUE_LIMIT),
    ('generation_failed', 'Reference tag not found in prompt', 'MAP_INTERNAL_REQUEST_TO_PROVIDER_REQUEST_FAILED', GENERATION_FAILED),
    ('generation_failed', 'InputImage contains a real person', 'FILE_OPTIMIZATION_FAILED', INPUT_PERSON),
    ('generation_failed', 'reference image: real_person', 'FILE_OPTIMIZATION_FAILED', REFERENCE_PERSON),
    ('generation_failed', 'OutputVideoSensitiveContentDetected copyright', 'PROVIDER_CONTENT_SAFETY_VIOLATION', OUTPUT_VIDEO_POLICY),
    ('generation_failed', 'InputTextSensitiveContentDetected', 'PROVIDER_CONTENT_SAFETY_VIOLATION', TEXT_POLICY),
    ('generation_failed', 'InputImageSensitiveContentDetected', 'FILE_OPTIMIZATION_FAILED', IMAGE_POLICY),
    ('validation_error', 'Artlist 素材总时长超限：audio_urls', '', MEDIA_DURATION),
    ('validation_error', 'Artlist 网页模型 image_urls 数量超限', '', MEDIA_LIMIT),
    ('validation_error', 'Artlist 素材格式不受支持：WAV', '', MEDIA_FORMAT),
    ('validation_error', '素材必须使用公网 HTTPS URL', '', MEDIA_EXTERNAL),
    ('media_unreachable', 'Artlist 上传素材不可读取', '', MEDIA_DOWNLOAD),
    ('submission_unknown', 'INSUFFICIENT_CREDITS', '', QUEUE_INTERRUPTED),
    ('upstream_outcome_unknown', 'Artlist deadline exceeded', '', QUEUE_INTERRUPTED),
])
def test_actual_failure_categories_choose_only_approved_literals(code, diagnostic, upstream, expected):
    assert public_message(code, diagnostic+' '+PRIVATE, upstream) == expected


def test_new_upstream_failures_store_safe_category_without_raw_reason():
    result = generation_result({'status':'Failed', 'errorCode':'FILE_OPTIMIZATION_FAILED',
                                'reason':'InputImage real person '+PRIVATE})
    assert result['public_error_message'] == INPUT_PERSON
    assert 'private-token' not in json.dumps(result)
    assert len(PUBLIC_MESSAGES) == 20
    for literal in PUBLIC_MESSAGES:
        assert public_message('generation_failed', literal) == literal


@pytest.mark.parametrize('path', PATHS)
def test_external_sync_errors_and_framework_failures_never_echo_diagnostics(api, path):
    app, http, headers = api
    @app.get('/v1/test-validation')
    async def validation_probe(value: int):
        return {'value':value}
    assert_public(http.get('/v1/test-validation', params={'value':PRIVATE}), 422)
    assert_public(http.post(path, json={}), 401, QUEUE_INTERRUPTED)
    assert_public(http.post(path, headers=headers, content='{bad Artlist json'), 422)
    assert_public(http.post(path, headers=headers, json=[]), 422)
    assert_public(http.post(path, headers=headers, json={'model':MODEL,'prompt':'test','idempotency_key':{}}), 422)
    assert_public(http.post(path, headers={**headers,'Content-Type':'application/octet-stream'}, content=PRIVATE), 422, MEDIA_EXTERNAL)
    assert_public(http.post(path, headers=headers, files={'file':('image.png',b'not an image')}), 422, MEDIA_EXTERNAL)
    assert_public(http.post(path, headers={**headers,'Origin':'https://foreign.example'}, json={}), 403, QUEUE_INTERRUPTED)
    assert_public(http.get(path+'/missing', headers=headers), 404)
    assert_public(http.put(path, headers=headers), 405)
    assert_public(http.get('/v1/missing'), 404)
    app.state.service.create = AsyncMock(side_effect=GatewayError(PRIVATE, 'entitlement_unavailable', 503))
    result = assert_public(http.post(path, headers=headers, json={}), 503, QUEUE_LIMIT)
    assert result['error']['code'] == 'entitlement_unavailable'
    app.state.service.create = AsyncMock(side_effect=GatewayError(PRIVATE, 'artlist_secret_error', 502))
    result = assert_public(http.post(path, headers=headers, json={}), 502)
    assert result['error']['code'] == 'upstream_error'
    app.state.service.create = AsyncMock(side_effect=RuntimeError(PRIVATE))
    assert_public(http.post(path, headers=headers, json={}), 500, QUEUE_INTERRUPTED)


def ready_account(db):
    account = db.save_account({'name':'test', 'proxy_url':'socks5://proxy:20001', 'backend':'web'})
    db.update_credentials(account['id'], {'web_cookie':'test=value'})
    db.update_account(account['id'], status='ready', profiles=profiles())
    db.save_account({'enabled':True}, account['id'])
    return account['id']


@pytest.mark.parametrize(('status', 'code', 'reason', 'upstream', 'expected'), [
    ('failed', 'generation_failed', 'Artlist 参考视频可能涉及版权限制', 'FILE_OPTIMIZATION_FAILED', VIDEO_POLICY),
    ('failed', 'generation_failed', 'Artlist 参考音频审核未通过', 'FILE_OPTIMIZATION_FAILED', CONTENT_POLICY),
    ('failed', 'generation_failed', 'Artlist 积分不足', 'INSUFFICIENT_CREDITS', QUEUE_LIMIT),
    ('submission_unknown', 'submission_unknown', PRIVATE, '', QUEUE_INTERRUPTED),
    ('submission_unknown', 'upstream_outcome_unknown', PRIVATE, '', QUEUE_INTERRUPTED),
])
def test_historical_tasks_replays_and_admin_details_are_separate(api, status, code, reason, upstream, expected):
    app, http, headers = api
    db = app.state.db
    aid = ready_account(db)
    payload = {'model':MODEL, 'prompt':'test', 'duration':5}
    task, _ = db.create_task(normalize_request(payload), [(aid, profiles()[MODEL])], 'original', 10)
    db.update_task(task['id'], status=status, error=reason, error_code=code, result={
        'upstream_error_code':upstream, 'preparation_timing':{'stage':PRIVATE},
        'media_processing':[{'actions':[PRIVATE]}], 'prompt_processing':{'action':PRIVATE},
        'public_error_message':'unapproved '+PRIVATE,
    })
    before = db.task(task['id'])
    for path in PATHS:
        result = assert_public(http.get(path+'/'+task['id'], headers=headers), 200, expected)
        assert result['error']['code'] == code and result['status'] == 'failed'
        assert all(key not in result for key in ('preparation_timing','media_processing','prompt_processing','upstream_error_code'))
        replay = assert_public(http.post(path, headers={**headers,'Idempotency-Key':'original'}, json=payload), 200, expected)
        assert replay['id'] == task['id']
        conflict = assert_public(http.post(path, headers={**headers,'Idempotency-Key':'original'}, json={**payload,'prompt':'changed'}), 409)
        assert conflict['error']['code'] == 'idempotency_conflict'
    app.state.service.schedule.assert_not_called()
    http.post('/login', data={'token':app.state.settings.admin_token})
    internal = http.get('/api/tasks').json()[0]
    assert internal['error']['message'] == reason
    assert internal['preparation_timing'] == before['result']['preparation_timing']
    assert db.task(task['id']) == before


def test_media_validation_is_not_misreported_as_queue_capacity(api):
    app, http, headers = api
    ready_account(app.state.db)
    payload = {'model':MODEL, 'prompt':'test', 'duration':5}
    for extra, expected in [
        ({'image_urls':['https://example.com/'+str(i)+'.png' for i in range(10)]}, MEDIA_LIMIT),
        ({'image_urls':['data:image/png;base64,aGVsbG8=']}, MEDIA_EXTERNAL),
        ({'duration':3}, MEDIA_DURATION),
    ]:
        assert_public(http.post(PATHS[0], headers=headers, json={**payload,**extra}), 422, expected)
    assert app.state.db.tasks() == []
    app.state.service.schedule.assert_not_called()


def test_successful_result_urls_and_task_identity_remain_usable(api):
    app, http, headers = api
    aid = ready_account(app.state.db)
    task, _ = app.state.db.create_task(normalize_request({'model':MODEL,'prompt':'test'}), [(aid, profiles()[MODEL])], 'success', 10)
    app.state.db.update_task(task['id'], status='succeeded', result={'video_url':'https://media.example/result.mp4'})
    for path in PATHS:
        result = http.get(path+'/'+task['id'], headers=headers).json()
        assert result['id']==task['id'] and result['status']=='succeeded' and result['error'] is None
        assert result['content']['video_url']==result['data'][0]['url']=='https://media.example/result.mp4'
