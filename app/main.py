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

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.catalog import PUBLIC_MODELS, profile_template
from app.config import Settings
from app.db import Database
from app.errors import GatewayError
from app.network import safe_error
from app.public_errors import public_error
from app.service import Service
from app.runtime_settings import RuntimePatch, apply_runtime, runtime_values


class AccountCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=120)
    proxy_url: str = Field(min_length=1, max_length=2000)
    max_concurrency: int = Field(default=1, ge=1, strict=True)
    backend: Literal['web', 'mcp'] = 'web'


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, min_length=1, max_length=120)
    proxy_url: str | None = Field(default=None, min_length=1, max_length=2000)
    max_concurrency: int | None = Field(default=None, ge=1, strict=True)
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
    apply_runtime(settings, db.runtime_settings())
    db.pin_task_deadlines(settings.task_timeout)
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

    def external_api(request):
        path = request.url.path
        return path in {'/v1', '/api/v3'} or path.startswith(('/v1/', '/api/v3/'))

    def error_response(request, code, message, status, headers=None):
        error = public_error(code, message) if external_api(request) else {'code': code, 'message': message}
        return JSONResponse({'error': error}, status_code=status, headers=headers)

    @app.middleware('http')
    async def security_headers(request, call_next):
        origin = request.headers.get('origin')
        if origin and request.method not in {'GET', 'HEAD'} and origin.rstrip('/') not in {settings.public_base_url, str(request.base_url).rstrip('/')}:
            if external_api(request):
                return error_response(request, 'forbidden', '', 403)
            return JSONResponse({'detail': 'cross-origin request denied'}, status_code=403)
        try:
            response = await call_next(request)
        except Exception as exc:
            if not external_api(request):
                raise
            db.event('api_request_failed', type(exc).__name__)
            response = error_response(request, 'internal_error', '', 500)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # no-referrer makes browser form POSTs send Origin: null, breaking the
        # same-origin login check. Keep the origin for local forms only.
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if not request.url.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        return error_response(request, exc.code, safe_error(exc), exc.status)

    @app.exception_handler(ValueError)
    async def validation_error(request, exc):
        return error_response(request, 'validation_error', safe_error(exc), 422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return error_response(request, 'not_found', str(exc).strip("'"), 404)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, exc):
        return error_response(request, 'conflict', '代理已绑定其他账号，或记录存在冲突', 409)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        if not external_api(request):
            return await http_exception_handler(request, exc)
        code = {401:'unauthorized', 403:'forbidden', 404:'not_found', 405:'method_not_allowed',
                429:'rate_limited'}.get(exc.status_code, 'upstream_error')
        return error_response(request, code, '', exc.status_code, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        if not external_api(request):
            return await request_validation_exception_handler(request, exc)
        return error_response(request, 'validation_error', '', 422)

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
        public_url = urlsplit(settings.public_base_url)
        # Direct LAN HTTP needs an HTTP cookie. Keep the canonical HTTPS host
        # secure through cloudflared even when the origin connection is HTTP.
        # A forwarded scheme can only tighten this decision, never downgrade it.
        secure_cookie = (
            request.url.scheme == 'https'
            or request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower() == 'https'
            or (public_url.scheme == 'https' and request.url.hostname == public_url.hostname)
        )
        response.set_cookie('art_session', session_value(int(time.time())+86400), httponly=True,
                            secure=secure_cookie, samesite='lax', max_age=86400)
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
                **runtime_values(settings), 'version': settings.version, 'chrome_executable': settings.chrome_executable,
                'model_ids': list(PUBLIC_MODELS), 'profile_template': profile_template()}

    @app.patch('/api/settings', dependencies=[Depends(admin)])
    async def save_runtime(payload: RuntimePatch):
        values = payload.model_dump(exclude_unset=True)
        if any(value is None for value in values.values()):
            raise ValueError('设置值不能为空')
        db.save_runtime_settings(values)
        apply_runtime(settings, values)
        if values:
            db.event('settings_saved', '已更新运行设置：' + ', '.join(values))
        return await runtime()

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
    async def tasks(limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
        return [{**service.public_task(task, internal=True), 'account_id': task['account_id'], 'proxy_version': task['proxy_version'],
                 'upstream_id': task['upstream_id'], 'internal_status': task['status'], 'request': task['request']} for task in db.tasks(limit=limit, offset=offset)]

    @app.get('/api/overview', dependencies=[Depends(admin)])
    async def overview():
        return db.task_summary()

    @app.delete('/api/tasks/completed', dependencies=[Depends(admin)])
    async def clear_completed_tasks():
        return {'cleared': db.clear_completed_tasks()}

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
        content_type = request.headers.get('content-type', '').split(';', 1)[0].lower()
        if content_type and content_type != 'application/json' and not content_type.endswith('+json'):
            raise ValueError('素材仅支持外链，暂不支持文件流/Base64等~')
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError('请求必须为 JSON 对象')
        key = request.headers.get('Idempotency-Key') or payload.pop('idempotency_key', '')
        if not isinstance(key, str) or len(key) > 200:
            raise ValueError('idempotency key too long')
        return await service.create(payload, key)

    @app.get('/v1/videos/{task_id}', dependencies=[Depends(api_key)])
    @app.get('/api/v3/contents/generations/tasks/{task_id}', dependencies=[Depends(api_key)])
    async def get_task(task_id: str):
        return service.public_task(db.task(task_id))

    return app
