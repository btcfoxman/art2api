import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.catalog import build_arguments, normalize_request, validate_profiles
from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.main import create_app
from app.network import client, normalize_proxy
from app.service import Service


MODEL = 'doubao-seedance-2-0-260128'
TOOLS = [
    {'name': 'video_create', 'inputSchema': {'type': 'object', 'properties': {
        'model': {'type': 'string'}, 'prompt': {'type': 'string'}, 'duration': {'enum': [5, 10]},
        'resolution': {'enum': ['720p']}, 'aspect_ratio': {'enum': ['16:9']},
        'images': {'type': 'array', 'maxItems': 1}}, 'required': ['model','prompt','duration','resolution','aspect_ratio'], 'additionalProperties': False}},
    {'name': 'video_status', 'inputSchema': {'type': 'object','properties': {'generation_id': {'type': 'string'}},'required':['generation_id']}},
]
PROFILE = {'submit_tool':'video_create','status_tool':'video_status','upstream_model':'seedance',
           'status_id_parameter':'generation_id', 'parameters':{k:k for k in ('model','prompt','duration','resolution','aspect_ratio')},
           'constraints':{'durations':[5,10],'resolutions':['720p'],'aspect_ratios':['16:9'],'max_images':0,'max_videos':0,'max_audios':0}}


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path,api_key='a'*32,admin_token='b'*32,encryption_key=Fernet.generate_key().decode(),poll_interval=1,task_timeout=60)


@pytest.fixture
def db(settings):
    value=Database(settings.data_dir/'test.db',settings.encryption_key)
    yield value
    value.close()


def ready(db, port=20001):
    account=db.save_account({'name':'Account','proxy_url':f'socks5://xray:{port}'})
    db.update_credentials(account['id'],{'access_token':'private-access-value','expires_at':time.time()+3600})
    db.update_account(account['id'],status='ready',egress_ip=f'203.0.113.{port-20000}',tools=TOOLS,profiles={MODEL:PROFILE})
    db.save_account({'enabled':True},account['id'])
    return db.account(account['id'])


def test_proxy_is_mandatory_and_client_ignores_environment(monkeypatch):
    for value in ('','direct','http://proxy','ftp://proxy:20','http://proxy:0','http://proxy:70000'):
        with pytest.raises(ValueError): normalize_proxy(value)
    monkeypatch.setenv('HTTP_PROXY','http://unrelated:1080')
    with patch('app.network.httpx.AsyncClient') as constructor:
        client('socks5://xray:20001')
        assert constructor.call_args.kwargs['proxy']=='socks5://xray:20001'
        assert constructor.call_args.kwargs['trust_env'] is False
        assert constructor.call_args.kwargs['follow_redirects'] is False


def test_credentials_encrypted_and_not_returned(db):
    account=ready(db)
    raw=db.conn.execute('SELECT secret FROM accounts').fetchone()[0]
    assert 'private-access-value' not in raw and 'socks5://' not in raw
    public=json.dumps(db.account(account['id']))
    assert 'private-access-value' not in public and 'credentials' not in public


def test_idempotency_pins_account_and_proxy(db):
    account=ready(db)
    request=normalize_request({'model':MODEL,'prompt':'sunrise','duration':5})
    task,created=db.create_task(request,[(account['id'],PROFILE)],'same-request',10)
    again,created_again=db.create_task(request,[],'same-request',10)
    assert created and not created_again and task['id']==again['id']
    with pytest.raises(GatewayError) as error:
        db.create_task({**request,'prompt':'different'},[],'same-request',10)
    assert error.value.code=='idempotency_conflict'
    with pytest.raises(ValueError): db.save_account({'proxy_url':'socks5://xray:20002'},account['id'])


def test_capacity_and_duplicate_exit_block_new_tasks(db):
    first=ready(db)
    second=ready(db,20002)
    db.update_account(second['id'],egress_ip=first['egress_ip'])
    request=normalize_request({'model':MODEL,'prompt':'sunrise','duration':5})
    with pytest.raises(GatewayError):
        db.create_task(request,[(first['id'],PROFILE),(second['id'],PROFILE)],'',10)


def test_capabilities_do_not_silently_drop_media_or_resolution():
    request=normalize_request({'model':MODEL,'prompt':'sunrise','duration':5})
    validate_profiles({MODEL:PROFILE},TOOLS)
    assert build_arguments(request,PROFILE,TOOLS)['duration']==5
    for changed in ({'image_urls':['https://example.com/a.png']},{'video_urls':['https://example.com/v.mp4']},{'resolution':'1080p'},{'duration':8},{'seed':1}):
        with pytest.raises(ValueError): build_arguments({**request,**changed},PROFILE,TOOLS)


@pytest.mark.asyncio
async def test_ambiguous_submission_is_never_resubmitted(db,settings):
    account=ready(db)
    service=Service(db,settings)
    fake=AsyncMock()
    fake.list_tools.return_value=TOOLS
    fake.call.side_effect=GatewayError('response interrupted','submission_unknown',ambiguous=True)
    service.mcp=lambda _:fake
    service.oauth.access_token=AsyncMock(return_value='token')
    public=await service.create({'model':MODEL,'prompt':'sunrise','duration':5},'request-1')
    await asyncio.gather(*list(service.jobs.values()))
    task=db.task(public['id'])
    assert task['status']=='submission_unknown'
    await service.create({'model':MODEL,'prompt':'sunrise','duration':5},'request-1')
    assert fake.call.await_count==1
    assert service.public_task(task)['error']['code']=='submission_unknown'


@pytest.mark.asyncio
async def test_success_and_restart_recovery_use_original_account(db,settings):
    account=ready(db)
    service=Service(db,settings)
    fake=AsyncMock()
    fake.list_tools.return_value=TOOLS
    fake.call.side_effect=[{'generation_id':'upstream-123'},{'status':'completed','video_url':'https://example.com/result.mp4'}]
    service.mcp=lambda account_id:fake if account_id==account['id'] else pytest.fail('wrong account')
    service.oauth.access_token=AsyncMock(return_value='token')
    public=await service.create({'model':MODEL,'prompt':'sunrise','duration':5})
    await asyncio.gather(*list(service.jobs.values()))
    assert db.task(public['id'])['status']=='succeeded'
    assert fake.call.await_args_list[1].args==('video_status',{'generation_id':'upstream-123'})


def test_api_and_admin_are_separate_and_metadata_has_no_secrets(settings):
    app=create_app(settings)
    with TestClient(app) as http:
        assert http.get('/health').json()['proxy_required'] is True
        assert http.get('/api/accounts',headers={'Authorization':'Bearer '+settings.api_key}).status_code==401
        assert http.get('/v1/models').status_code==401
        assert http.get('/v1/models',headers={'Authorization':'Bearer '+settings.api_key}).json()['data']==[]
        response=http.get('/oauth/client-metadata.json')
        assert settings.admin_token not in response.text and settings.encryption_key not in response.text
        http.post('/login',data={'token':settings.admin_token})
        assert http.post('/api/accounts',json={'name':'a','proxy_url':'socks5://xray:20001'}).status_code==403
        assert http.post('/api/accounts',headers={'X-Requested-With':'art2api'},json={'name':'a','proxy_url':''}).status_code==422
