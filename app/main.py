from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.catalog import PUBLIC_MODELS, profile_template
from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.network import safe_error
from app.service import Service


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    proxy_url: str = Field(min_length=1, max_length=2000)
    max_concurrency: int = Field(default=1, ge=1, le=20)
    backend: Literal['web', 'mcp'] = 'web'


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=120)
    proxy_url: str | None = Field(default=None, min_length=1, max_length=2000)
    max_concurrency: int | None = Field(default=None, ge=1, le=20)
    enabled: bool | None = None
    backend: Literal['web', 'mcp'] | None = None


class WebSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cookie: str = Field(min_length=1, max_length=100000)
    user_agent: str = Field(min_length=1, max_length=1000)
    team_id: str = Field(default='', max_length=100)


class WebVerification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=20, max_length=16000)


class BrowserAction(BaseModel):
    kind: str
    x: float = Field(default=0, ge=0, le=1100)
    y: float = Field(default=0, ge=-760, le=760)
    text: str = Field(default='', max_length=2000)
    key: str = ''


def create_app(settings=None):
    settings = settings or Settings()
    settings.validate()
    db = Database(settings.data_dir / 'art2api.db', settings.encryption_key)
    service = Service(db, settings)
    static = Path(__file__).parent / 'static'

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        yield
        await service.stop()
        db.close()

    app = FastAPI(title='ART2API', version=settings.version, lifespan=lifespan,
                  description='Artlist web/MCP Seedance gateway with mandatory per-account proxies')
    app.state.service, app.state.db, app.state.settings = service, db, settings
    app.mount('/static', StaticFiles(directory=static), name='static')

    def session_value(expiry):
        value = str(expiry)
        return value + '.' + hmac.new(settings.admin_token.encode(), value.encode(), hashlib.sha256).hexdigest()

    def is_admin(request):
        value = request.cookies.get('art_session', '')
        try:
            expiry = int(value.split('.')[0])
        except ValueError:
            return False
        return expiry > time.time() and hmac.compare_digest(value, session_value(expiry))

    def admin(request: Request):
        if not is_admin(request):
            raise HTTPException(401, 'admin login required')
        if request.method not in {'GET', 'HEAD'} and request.headers.get('X-Requested-With') != 'art2api':
            raise HTTPException(403, 'missing same-origin request marker')

    def api_key(request: Request):
        header = request.headers.get('Authorization', '')
        token = header[7:].strip() if header.lower().startswith('bearer ') else request.headers.get('X-API-Key', '')
        if not token or not hmac.compare_digest(token, settings.api_key):
            raise HTTPException(401, 'invalid API key')

    @app.middleware('http')
    async def security_headers(request, call_next):
        origin = request.headers.get('origin')
        if origin and request.method not in {'GET', 'HEAD'} and origin.rstrip('/') not in {settings.public_base_url, str(request.base_url).rstrip('/')}:
            return JSONResponse({'detail': 'cross-origin request denied'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # no-referrer makes browser form POSTs send Origin: null, breaking the
        # same-origin login check. Keep the origin for local forms only.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(_, exc):
        return JSONResponse({'error': {'code': exc.code, 'message': safe_error(exc)}}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def validation_error(_, exc):
        return JSONResponse({'error': {'code': 'validation_error', 'message': safe_error(exc)}}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(_, exc):
        return JSONResponse({'error': {'code': 'not_found', 'message': str(exc).strip("'")}}, status_code=404)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(_, exc):
        return JSONResponse({'error': {'code': 'conflict', 'message': '代理已绑定其他账号，或记录存在冲突'}}, status_code=409)

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'version': settings.version, 'proxy_required': True}

    @app.get('/login')
    async def login_page():
        return FileResponse(static / 'login.html')

    @app.post('/login')
    async def login(request: Request, token: str = Form(...)):
        if not hmac.compare_digest(token, settings.admin_token):
            return RedirectResponse('/login?error=1', status_code=303)
        response = RedirectResponse('/', status_code=303)
        response.set_cookie('art_session', session_value(int(time.time())+86400), httponly=True,
                            secure=settings.public_base_url.startswith('https://'), samesite='lax', max_age=86400)
        return response

    @app.post('/logout', dependencies=[Depends(admin)])
    async def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie('art_session')
        return response

    @app.get('/')
    async def dashboard(request: Request):
        return FileResponse(static / 'index.html') if is_admin(request) else RedirectResponse('/login', 303)

    @app.get('/api/settings', dependencies=[Depends(admin)])
    async def runtime():
        return {'mcp_url': settings.mcp_url, 'public_base_url': settings.public_base_url, 'proxy_required': True,
                'task_timeout_seconds': settings.task_timeout, 'poll_interval_seconds': settings.poll_interval,
                'queue_limit': settings.queue_limit, 'model_ids': list(PUBLIC_MODELS), 'profile_template': profile_template()}

    @app.get('/api/accounts', dependencies=[Depends(admin)])
    async def accounts():
        return db.accounts()

    @app.post('/api/accounts', dependencies=[Depends(admin)])
    async def add_account(payload: AccountCreate):
        return db.save_account(payload.model_dump())

    @app.patch('/api/accounts/{account_id}', dependencies=[Depends(admin)])
    async def patch_account(account_id: str, payload: AccountPatch):
        if payload.proxy_url:
            await service.browsers.close(account_id)
        return db.save_account(payload.model_dump(exclude_none=True), account_id)

    @app.delete('/api/accounts/{account_id}', dependencies=[Depends(admin)])
    async def delete_account(account_id: str):
        db.delete_account(account_id)
        await service.browsers.close(account_id)
        return {'ok': True}

    @app.post('/api/accounts/{account_id}/check', dependencies=[Depends(admin)])
    async def check(account_id: str):
        return await service.inspect_account(account_id)

    @app.post('/api/accounts/{account_id}/connect', dependencies=[Depends(admin)])
    async def connect(account_id: str):
        try:
            url = 'https://toolkit.artlist.io/image-video-generator?mode=video' if db.account(account_id)['backend']=='web' else await service.oauth.begin(account_id)
            await service.browsers.open(account_id, url)
        except GatewayError as exc:
            db.update_account(account_id, enabled=False, status='oauth_client_required' if exc.code == 'oauth_client_required' else 'error', last_error=safe_error(exc))
            raise
        return {'status': 'browser_ready', 'account_id': account_id}

    @app.put('/api/accounts/{account_id}/web-session', dependencies=[Depends(admin)])
    async def import_web_session(account_id: str, payload: WebSession):
        return await service.import_web_session(account_id, payload.model_dump())

    @app.post('/api/accounts/{account_id}/browser/save-session', dependencies=[Depends(admin)])
    async def save_browser_session(account_id: str):
        if db.account(account_id)['backend']!='web':
            raise ValueError('仅网页账号可保存网页登录')
        payload = await service.browsers.session_credentials(account_id)
        return await service.import_web_session(account_id, payload)

    @app.post('/api/accounts/{account_id}/web-verification', dependencies=[Depends(admin)])
    async def web_verification(account_id: str, payload: WebVerification):
        db.save_verification(account_id, payload.token)
        return db.account(account_id)

    @app.get('/api/accounts/{account_id}/web-tasks/{generation_id}', dependencies=[Depends(admin)])
    async def web_task(account_id: str, generation_id: str):
        if db.account(account_id)['backend']!='web':
            raise ValueError('仅网页账号支持此查询')
        return await service.web(account_id).query(generation_id)

    @app.post('/api/accounts/{account_id}/web-quote', dependencies=[Depends(admin)])
    async def web_quote(account_id: str, request: Request):
        from app.catalog import normalize_request
        payload = normalize_request(await request.json())
        if any(payload[k] for k in ('image_urls','video_urls','audio_urls')) or payload.get('first_frame'):
            raise ValueError('单独报价检测仅用于无素材请求；正式任务会上传素材后重新报价')
        quote, _, _, _ = await service.web(account_id).quote(payload)
        return {'model': payload['model'], 'resolved_model_id':quote['modelId'], 'feature':quote['modelFeature'], 'credits':quote['cost'], 'validation':'quote_verified'}

    @app.get('/api/accounts/{account_id}/browser', dependencies=[Depends(admin)])
    async def browser(account_id: str):
        return Response(await service.browsers.snapshot(account_id), media_type='image/jpeg')

    @app.post('/api/accounts/{account_id}/browser', dependencies=[Depends(admin)])
    async def browser_action(account_id: str, payload: BrowserAction):
        await service.browsers.action(account_id, payload.model_dump())
        return {'ok': True}

    @app.delete('/api/accounts/{account_id}/browser', dependencies=[Depends(admin)])
    async def close_browser(account_id: str):
        await service.browsers.close(account_id)
        return {'ok': True}

    @app.get('/oauth/callback')
    async def callback(request: Request):
        if request.query_params.get('error'):
            return HTMLResponse('<h2>Artlist 授权未完成，请关闭此窗口后重新连接。</h2>', status_code=400)
        account_id = await service.oauth.callback(request.query_params.get('state', ''), request.query_params.get('code', ''))
        # Keep the proxied browser on this confirmation page until the operator closes it.
        return HTMLResponse('<meta charset="utf-8"><h2>Artlist 授权成功</h2><p>返回控制台，关闭授权窗口并点击“检测连接”以读取模型工具。</p>')

    @app.get('/oauth/client-metadata.json')
    async def client_metadata():
        return {'client_id': settings.public_base_url + '/oauth/client-metadata.json', 'client_name': 'ART2API',
                'client_uri': settings.public_base_url,
                'redirect_uris': [settings.public_base_url + '/oauth/callback'],
                'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code'],
                'token_endpoint_auth_method': 'none'}

    @app.post('/api/accounts/{account_id}/catalog', dependencies=[Depends(admin)])
    async def catalog(account_id: str, request: Request):
        payload = await request.json()
        return await service.catalog(account_id, payload['tool'], payload.get('arguments', {}))

    @app.put('/api/accounts/{account_id}/profiles', dependencies=[Depends(admin)])
    async def profiles(account_id: str, request: Request):
        return service.save_profiles(account_id, await request.json())

    @app.get('/api/tasks', dependencies=[Depends(admin)])
    async def tasks():
        return [{**service.public_task(task), 'account_id': task['account_id'], 'proxy_version': task['proxy_version'],
                 'upstream_id': task['upstream_id'], 'internal_status': task['status'], 'request': task['request']} for task in db.tasks()]

    @app.post('/api/tasks', dependencies=[Depends(admin)])
    async def test_task(request: Request):
        return await service.create(await request.json(), request.headers.get('Idempotency-Key', ''))

    @app.post('/api/tasks/{task_id}/recover', dependencies=[Depends(admin)])
    async def recover(task_id: str, request: Request):
        return await service.recover_unknown(task_id, (await request.json()).get('upstream_id', ''))

    @app.get('/api/events', dependencies=[Depends(admin)])
    async def events():
        return db.events()

    @app.get('/v1/models', dependencies=[Depends(api_key)])
    async def models():
        return service.models()

    @app.post('/v1/videos', dependencies=[Depends(api_key)])
    @app.post('/api/v3/contents/generations/tasks', dependencies=[Depends(api_key)])
    async def generate(request: Request):
        payload = await request.json()
        key = request.headers.get('Idempotency-Key') or payload.pop('idempotency_key', '')
        if len(key) > 200:
            raise ValueError('idempotency key too long')
        return await service.create(payload, key)

    @app.get('/v1/videos/{task_id}', dependencies=[Depends(api_key)])
    @app.get('/api/v3/contents/generations/tasks/{task_id}', dependencies=[Depends(api_key)])
    async def get_task(task_id: str):
        return service.public_task(db.task(task_id))

    return app
