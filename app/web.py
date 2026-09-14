from __future__ import annotations

import asyncio
import json
import mimetypes
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from fractions import Fraction
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from jsonschema import Draft202012Validator, ValidationError

from app.catalog import local_schema
from app.cookies import parse_cookie_header
from app.errors import GatewayError
from app.media import adapt_reference_video, append_audio_silence, audio_silence_plan, fps_in_range, probe
from app.network import client, public_media_url
from app.web_catalog import GROUPS, reference_header, generation_payload, generation_result, profiles, quote_input, reference_prompt, validate_request

BASE = 'https://toolkit.artlist.io'
PROCEDURES = {
    'dynamicPromptSettings.getDynamicPromptSettings', 'modelRouter.getModelGroups',
    'modelRouter.getModel', 'modelRouter.getCostQuote', 'userGenerationRouter.checkGenerationEligibility',
    'uploadRouter.getPresignedUrl', 'uploadRouter.getPresignedUrlFromKey', 'chatSession.createChatSession',
    'userGenerationRouter.createUserGeneration', 'userGenerationRouter.getUserGenerationById',
    'userGenerationRouter.getUserGenerationOutputById', 'userGenerationRouter.getUserGenerationsBySession',
}
# Only observed read-only operations may be repeated after a transport failure.
# Some tRPC queries use POST; HTTP method alone cannot establish replay safety.
SAFE_RETRY_METHODS = {
    '/api/auth/session': 'GET',
    **{'/api/trpc/'+name: 'GET' for name in (
        'dynamicPromptSettings.getDynamicPromptSettings', 'modelRouter.getModelGroups',
        'modelRouter.getModel', 'userGenerationRouter.getUserGenerationById',
        'userGenerationRouter.getUserGenerationOutputById',
        'userGenerationRouter.getUserGenerationsBySession',
    )},
    **{'/api/trpc/'+name: 'POST' for name in (
        'modelRouter.getCostQuote', 'userGenerationRouter.checkGenerationEligibility',
        'uploadRouter.getPresignedUrl', 'uploadRouter.getPresignedUrlFromKey',
    )},
}
TRANSIENT_TRANSPORT_ERRORS = (
    httpx.TimeoutException, httpx.NetworkError, httpx.ProxyError, httpx.RemoteProtocolError,
)
web_task_context = ContextVar('web_task_context', default=None)
preparation_context = ContextVar('preparation_context', default=None)
media_context = ContextVar('media_context', default=None)


class NoStoredCookies(DefaultCookiePolicy):
    # Session cookies come from the encrypted DB, never a pooled client's jar.
    # Download hosts must not plant cookies for later upload requests either.
    def set_ok(self, cookie, request):
        return False


@contextmanager
def measure(record, key):
    started = time.monotonic()
    try:
        yield
    finally:
        record[key] = round(record.get(key, 0) + time.monotonic() - started, 3)


async def file_io(function, *args):
    # Wait for in-flight disk work on cancellation before removing its temp dir.
    job = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        await asyncio.gather(job, return_exceptions=True)
        raise


def rejection_reason(response):
    """Expose recognized public error codes, never arbitrary upstream text."""
    fallback = 'upstream_bad_request' if response.status_code == 400 else 'upstream_forbidden'
    try:
        error = response.json().get('error', {})
        error = error.get('json', error)
        message = str(error.get('message', ''))
        if response.status_code == 400:
            try:
                issues = json.loads(message)
            except (ValueError, TypeError):
                issues = []
            if isinstance(issues, list) and any(isinstance(issue, dict) and
                    issue.get('path') == ['teamId'] and issue.get('code') == 'invalid_type' and
                    issue.get('received') == 'undefined' for issue in issues):
                return 'MISSING_SESSION_TEAM_ID'
        for code in ('TURNSTILE_VERIFICATION_FAILED', 'FREE_TIER_GENERATION_BLOCKED',
                     'INSUFFICIENT_CREDITS', 'UNAUTHORIZED', 'FORBIDDEN'):
            if code in message:
                return code
        return fallback
    except (ValueError, AttributeError):
        return fallback


def unwrap(body):
    if not isinstance(body, dict) or 'error' in body:
        raise GatewayError('Artlist 网页接口拒绝请求', 'generation_rejected', 422)
    try:
        value = body['result']['data']['json']
    except (KeyError, TypeError):
        if body.get('success') is False:
            raise GatewayError('Artlist 网页校验未通过', 'generation_rejected', 422)
        raise GatewayError('Artlist 网页协议响应结构已变化', 'web_protocol_error') from None
    if isinstance(value, dict) and value.get('success') is False:
        raise GatewayError('Artlist 明确拒绝此次请求', 'generation_rejected', 422)
    return value.get('data', value) if isinstance(value, dict) else value


class WebClient:
    """Captured tRPC protocol. Account cookies, media and queries share one fixed proxy."""
    def __init__(self, account_id, db, settings, browsers=None):
        self.account_id, self.db, self.settings = account_id, db, settings
        self.lock = asyncio.Lock()
        self.browsers = browsers
        self.proxy_version = self.db.account(account_id)['proxy_version']
        self.http = None
        self.media_http = None
        self.media_slots = asyncio.Semaphore(12)
        self.model_lock = asyncio.Lock()
        self.model_cache = {}

    def pooled_client(self, *, media=False):
        account = self.db.account(self.account_id, True)
        if account['proxy_version'] != self.proxy_version:
            raise GatewayError('账号固定代理已变更', 'proxy_binding_changed')
        attr = 'media_http' if media else 'http'
        http = getattr(self, attr)
        if http is None:
            http = client(account['credentials']['proxy_url'], self.settings.request_timeout,
                          cookies=CookieJar(policy=NoStoredCookies()),
                          limits=httpx.Limits(max_connections=12 if media else 1,
                                             max_keepalive_connections=12 if media else 1,
                                             keepalive_expiry=60))
            setattr(self, attr, http)
        return http

    @asynccontextmanager
    async def media_client(self):
        yield self.pooled_client(media=True)

    async def aclose(self):
        for attr in ('http', 'media_http'):
            http = getattr(self, attr)
            if http is not None:
                await http.aclose()
                setattr(self, attr, None)
        self.model_cache.clear()

    def secret(self):
        return self.db.account(self.account_id, True)['credentials']

    async def request(self, method, path, **kwargs):
        method = method.upper()
        attempts = 3 if SAFE_RETRY_METHODS.get(path) == method else 1
        account = self.db.account(self.account_id, True)
        binding = (account['proxy_version'], account['credentials'].get('web_user_id'))
        started = time.monotonic()
        for attempt in range(1, attempts+1):
            timing = preparation_context.get()
            metric = timing.setdefault('requests', {}).setdefault(path.rsplit('/', 1)[-1], {}) if timing is not None else {}
            try:
                with measure(metric, 'queue_seconds'):
                    await self.lock.acquire()
                try:
                    current = self.db.account(self.account_id, True)
                    if (current['proxy_version'], current['credentials'].get('web_user_id')) != binding:
                        raise GatewayError('请求重试期间账号或固定代理已变更', 'proxy_binding_changed')
                    metric['calls'] = metric.get('calls', 0) + 1
                    with measure(metric, 'request_seconds'):
                        response = await self._request(method, path, **kwargs)
                finally:
                    self.lock.release()
            except httpx.TransportError as exc:
                retry = isinstance(exc, TRANSIENT_TRANSPORT_ERRORS) and attempt < attempts
                # Exception strings can contain proxy credentials, cookies or
                # signed inputs. Emit only the fixed endpoint and class name.
                detail = (f'Artlist 网页代理请求失败（{method} {path.rsplit("/",1)[-1]}；'
                          f'{type(exc).__name__}；第 {attempt} 次；累计 {time.monotonic()-started:.2f} 秒）')
                self.db.event('web_request_retry' if retry else 'web_request_failed',
                              detail, self.account_id, web_task_context.get())
                if not retry:
                    raise GatewayError(detail, 'proxy_error', 502,
                                       retryable=isinstance(exc, TRANSIENT_TRANSPORT_ERRORS)) from None
                # Release the account cookie lock before waiting, so other
                # tasks can continue polling through the same fixed proxy.
                await asyncio.sleep(attempt)
            else:
                if attempt > 1:
                    self.db.event('web_request_recovered',
                                  f'{method} {path.rsplit("/",1)[-1]}：第 {attempt} 次恢复，累计 {time.monotonic()-started:.2f} 秒',
                                  self.account_id, web_task_context.get())
                return response

    async def _request(self, method, path, **kwargs):
        secret = self.secret()
        if not secret.get('web_cookie'):
            raise GatewayError('需要登录 Artlist 网页账号', 'reauthorization_required', 401)
        headers = {'Cookie': secret['web_cookie'], 'User-Agent': secret.get('web_user_agent', ''),
                   'Origin': BASE, 'Referer': BASE+'/', 'x-trpc-source': 'react', 'x-request-id': str(uuid.uuid4())}
        if path != '/api/auth/session' and (not path.startswith('/api/trpc/') or path.removeprefix('/api/trpc/') not in PROCEDURES):
            raise ValueError('网页接口不在已采集的接口清单中')
        timeout = self.settings.request_timeout
        response = await self.pooled_client().request(method, BASE+path, headers=headers,
                         timeout=httpx.Timeout(timeout, connect=min(timeout, 20)), **kwargs)
        if response.status_code == 401:
            self.db.update_account(self.account_id, enabled=False, status='unauthorized', last_error='网页登录已失效，请重新登录')
            raise GatewayError('Artlist 网页登录已失效', 'reauthorization_required', 401)
        if response.status_code in {400, 403}:
            self.db.update_credentials(self.account_id, {'web_last_rejection': {
                'procedure': path.rsplit('/',1)[-1], 'status': response.status_code,
                'task_id': web_task_context.get(), 'received_at': time.time(),
                'content_type': response.headers.get('content-type', ''), 'body': response.text[:16000],
            }})
            reason = rejection_reason(response)
            message = f'Artlist 拒绝网页协议请求（HTTP {response.status_code}，{reason}，{path.rsplit("/",1)[-1]}）'
            self.db.update_account(self.account_id, last_error=message)
            self.db.event('web_request_rejected', message, self.account_id, web_task_context.get())
            raise GatewayError(message, 'generation_rejected', 422)
        if response.status_code >= 500:
            raise GatewayError('Artlist 网页上游服务异常', 'web_upstream_error', 502, retryable=True)
        if response.status_code >= 400:
            raise GatewayError(f'Artlist 网页拒绝请求（HTTP {response.status_code}）', 'generation_rejected', 422)
        if response.is_redirect:
            raise GatewayError('Artlist 网页会话需要重新登录', 'reauthorization_required', 401)
        try:
            body = response.json()
        except ValueError:
            raise GatewayError('Artlist 网页返回了非 JSON 响应，请检查网页验证', 'web_protocol_error') from None
        # Persist rotated session cookies without exposing them in account responses.
        if response.headers.get_list('set-cookie'):
            jar = parse_cookie_header(secret['web_cookie'])
            for value in response.headers.get_list('set-cookie'):
                updates = SimpleCookie()
                updates.load(value)
                for name, morsel in updates.items():
                    if not morsel.value or morsel['max-age'] == '0':
                        jar.pop(name, None)
                    else:
                        jar[name] = morsel.value
            self.db.update_web_session_if_current(self.account_id, secret,
                {'cookie': '; '.join(f'{k}={v}' for k,v in jar.items()),
                 'user_agent': secret.get('web_user_agent', '')})
        return body

    async def rpc(self, name, value=None, *, post=False):
        envelope = {'json': value}
        if value is None:
            envelope['meta'] = {'values': ['undefined']}
        kwargs = {'json': envelope} if post else {'params': {'input': json.dumps(envelope, separators=(',', ':'))}}
        return unwrap(await self.request('POST' if post else 'GET', '/api/trpc/'+name, **kwargs))

    async def check(self):
        session = await self.request('GET', '/api/auth/session')
        if not isinstance(session, dict) or not session.get('user', {}).get('id'):
            raise GatewayError('Artlist 网页账号未登录', 'reauthorization_required', 401)
        if self.secret().get('web_user_id') != session['user']['id']:
            raise GatewayError('网页账号身份已变化，请重新导入会话', 'reauthorization_required', 401)
        expiry = session.get('expires')
        if expiry:
            try:
                self.db.update_credentials(self.account_id, {'expires_at': datetime.fromisoformat(expiry.replace('Z','+00:00')).timestamp()})
            except (ValueError, TypeError):
                pass
        groups = await self.rpc('modelRouter.getModelGroups')
        live = {m['id']: m for category in groups for m in category.get('modelGroups', [])}
        available = {model: p for model,p in profiles().items() if p['group_id'] in live and not live[p['group_id']].get('disableGenerations') and not live[p['group_id']].get('isComingSoon')}
        self.db.update_account(self.account_id, profiles=available, tools=[], status='ready', last_error='',
                               catalog={'source': 'web_cdp_capture', 'model_groups': [{'id': gid, 'name': live[gid]['name']} for gid in set(GROUPS.values()) if gid in live]})
        return available

    async def model_definition(self, model_id, *, refresh=False):
        # Only cache schema definitions. Cost signatures, eligibility, session
        # and verification results must be obtained for each generation.
        async with self.model_lock:
            identity = self.secret().get('web_user_id')
            key = (identity, model_id)
            cached = self.model_cache.get(key)
            if cached and not refresh and time.monotonic() - cached[0] < 300:
                timing = preparation_context.get()
                if timing is not None:
                    timing['model_cache_hits'] = timing.get('model_cache_hits', 0) + 1
                return cached[1], True
            model = await self.rpc('modelRouter.getModel', {'modelId': model_id})
            if len(self.model_cache) >= 128:
                self.model_cache.pop(next(iter(self.model_cache)))
            self.model_cache[key] = (time.monotonic(), model)
            return model, False

    async def quote(self, request, assets=None, *, allow_short_audio=False):
        validate_request(request, profiles()[request['model']])
        payload, inputs, settings, artifacts = quote_input(request, assets or {})
        quote = await self.rpc('modelRouter.getCostQuote', payload, post=True)
        if not isinstance(quote, dict) or not all(k in quote for k in ('modelId','cost','modelFeature','digitalSignature','timestamp')):
            raise GatewayError('Artlist 未返回完整报价签名或子模型 ID', 'web_protocol_error')
        model, cached = await self.model_definition(quote['modelId'])
        schema_input = {**settings, **{k:([x['fileUrl'] for x in v] if isinstance(v,list) and v and isinstance(v[0],dict) and 'fileUrl' in v[0] else v['fileUrl'] if isinstance(v,dict) and 'fileUrl' in v else v) for k,v in inputs.items()}}
        valid = False
        for attempt in range(2):
            group_matches = model.get('modelGroupId') == GROUPS[request['model']]
            schemas = [c.get('internalConfig', {}).get('properties', {}).get('input') for c in model.get('configs', [])]
            for schema in schemas if group_matches else []:
                if not schema:
                    continue
                try:
                    Draft202012Validator(local_schema(schema)).validate(schema_input)
                    valid = True
                    break
                except ValidationError:
                    continue
            if valid or not cached or attempt:
                break
            model, _ = await self.model_definition(quote['modelId'], refresh=True)
        if not group_matches:
            raise GatewayError('报价返回的子模型与请求模型组不一致', 'web_protocol_error')
        if not valid:
            raise ValueError('请求参数不符合 Artlist 当前所选子模型的定义')
        self.validate_media(assets or {}, quote.get('modelContextConfig', {}), allow_short_audio=allow_short_audio)
        return quote, inputs, settings, artifacts

    @staticmethod
    def validate_media(assets, context, *, allow_short_audio=False):
        for field, kind in [('image_urls','Image'), ('video_urls','Video'), ('audio_urls','Audio')]:
            values = assets.get(field, [])
            if values and context.get('isSupport'+kind+'Upload') is False:
                raise ValueError('Artlist 子模型不支持 '+field)
            maximum = context.get('max'+kind+'InputCount')
            if maximum is not None and len(values)>maximum:
                raise ValueError('Artlist 子模型素材数量超限：'+field)
            if kind == 'Image':
                labelled = [(f'图{index}', value) for index, value in enumerate(values, 1)]
                labelled += [(label, value) for key, label in [('first_frame', '首帧'), ('last_frame', '尾帧')] for value in assets.get(key, [])]
                values = [value for _, value in labelled]
                lower, upper = context.get('minImageResolutionPx'), context.get('maxImageResolutionPx')
                invalid = []
                for label, value in labelled:
                    width, height = value['metadata'].get('width', 0), value['metadata'].get('height', 0)
                    if (lower and min(width, height) < lower) or (upper and max(width, height) > upper):
                        invalid.append(f'{label}（{width}×{height}）')
                if invalid:
                    bounds = '、'.join(filter(None, [f'短边至少 {lower}px' if lower else '', f'长边至多 {upper}px' if upper else '']))
                    raise ValueError(f'Artlist 图片尺寸不受支持：{"、".join(invalid)}；要求{bounds}')
            for index, asset in enumerate(values, 1):
                meta=asset['metadata']
                label = {'Image': '图片', 'Video': '视频', 'Audio': '音频'}[kind] + f' {index}'
                limit=context.get('maxUploaded'+kind+'SizeMB')
                if limit and meta['byteSize']>limit*1024*1024:
                    raise ValueError('Artlist 素材文件大小超限：'+field)
                duration=meta.get('durationMs',0)/1000
                formats = context.get(kind.lower()+'Formats') or []
                extension = Path(meta.get('fileName', '')).suffix.lstrip('.').upper()
                extension = {'JPEG':'JPG', 'M4A':'MP4', 'WAVE':'WAV'}.get(extension, extension)
                if formats and extension and extension not in formats:
                    raise ValueError(f'Artlist 素材格式不受支持：{label} 为 {extension}；允许 {"、".join(formats)}')
                if kind == 'Video' and meta.get('fps'):
                    lower, upper = context.get('minVideoFps'), context.get('maxVideoFps')
                    if not fps_in_range(meta['fps'], lower, upper):
                        bounds = '、'.join(filter(None, [f'至少 {lower:g}fps' if lower else '', f'最多 {upper:g}fps' if upper else '']))
                        raise ValueError(f'Artlist 参考视频帧率不受支持：视频 {index} 为 {meta["fps"]:g}fps；要求{bounds}')
                for key, compare in [('minUploaded'+kind+'Duration', lambda n: duration<n), ('maxUploaded'+kind+'Duration', lambda n: duration>n)]:
                    if allow_short_audio and key == 'minUploadedAudioDuration':
                        continue
                    if context.get(key) and compare(context[key]):
                        bound = '至少' if key.startswith('min') else '最多'
                        raise ValueError(f'Artlist 素材时长不受支持：{label} 为 {duration:g} 秒，要求{bound} {context[key]:g} 秒')
            total=sum(a['metadata'].get('durationMs',0)/1000 for a in values)
            if kind == 'Audio' and values and not allow_short_audio and total < (context.get('minTotalAudioDuration') or 0):
                raise ValueError(f'Artlist 音频总时长不足：当前 {total:g} 秒，要求至少 {context["minTotalAudioDuration"]:g} 秒')
            limit=context.get('maxTotal'+kind+'Duration') or context.get('total'+kind+'InputDuration')
            if limit and total>limit:
                raise ValueError('Artlist 素材总时长超限：'+field)
        count=sum(len(v) for v in assets.values())
        if context.get('totalInputLimit') and count>context['totalInputLimit']:
            raise ValueError('Artlist 子模型素材总数超限')
        for field, flag in [('first_frame','hasStartFrame'), ('last_frame','hasEndFrame')]:
            if assets.get(field) and context.get(flag) is False:
                raise ValueError('Artlist 子模型不支持首尾帧设置')

    async def upload(self, url, kind, *, reference_request=None, silence_seconds=0):
        record = media_context.get()
        if record is None:
            record = {}
        with measure(record, 'total_seconds'):
            with measure(record, 'queue_seconds'):
                await self.media_slots.acquire()
            try:
                return await self._upload(url, kind, record, reference_request=reference_request,
                                          silence_seconds=silence_seconds)
            finally:
                self.media_slots.release()

    async def _upload(self, url, kind, record, *, reference_request=None, silence_seconds=0):
        public_media_url(url)
        timeout = httpx.Timeout(max(120, self.settings.request_timeout), connect=20)
        async with self.media_client() as http:
            with measure(record, 'download_seconds'):
                for _ in range(4):
                    async with http.stream('GET', url, timeout=timeout) as response:
                        if response.is_redirect:
                            url = public_media_url(urljoin(url, response.headers.get('location','')))
                            continue
                        if response.status_code >= 400:
                            raise ValueError('参考素材下载失败')
                        mime = response.headers.get('content-type','').split(';')[0].strip().lower()
                        if not mime.startswith(kind+'/'):
                            mime = mimetypes.guess_type(urlsplit(url).path)[0] or ''
                        if not mime.startswith(kind+'/'):
                            raise ValueError('参考素材类型不匹配')
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content)>150*1024*1024:
                                raise ValueError('参考素材超过 150 MiB')
                        break
                else:
                    raise ValueError('参考素材重定向次数过多')
            record['download_bytes'] = len(content)
            # Linux mime databases often know audio/x-wav but omit audio/wav.
            # Wire filenames must be stable across the developer OS and Docker.
            suffix = {
                'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
                'image/gif': '.gif', 'image/webp': '.webp',
                'video/mp4': '.mp4', 'video/quicktime': '.mov',
                'audio/wav': '.wav', 'audio/x-wav': '.wav', 'audio/wave': '.wav',
                'audio/vnd.wave': '.wav', 'audio/mpeg': '.mp3', 'audio/mp3': '.mp3',
                'audio/x-mp3': '.mp3', 'audio/mp4': '.m4a', 'audio/x-m4a': '.m4a',
            }.get(mime) or mimetypes.guess_extension(mime) or '.bin'
            filename = uuid.uuid4().hex+suffix
            metadata = {'mimeType': mime, 'fileName': filename, 'byteSize': len(content)}
            processing = None
            with tempfile.TemporaryDirectory(prefix='art-media-') as directory:
                path = Path(directory)/filename
                with measure(record, 'probe_seconds'):
                    await file_io(path.write_bytes, content)
                    media = await probe(path)
                visual=next((s for s in media.get('streams',[]) if s.get('codec_type')=='video'),{})
                if kind in {'image','video'}:
                    metadata.update(width=visual.get('width',0),height=visual.get('height',0))
                if kind in {'audio','video'}:
                    metadata['durationMs']=round(float(media.get('format',{}).get('duration',0))*1000)
                    if metadata['durationMs']<=0:
                        raise ValueError('参考音视频缺少有效时长')
                if kind == 'video':
                    try:
                        metadata['fps'] = float(Fraction(visual.get('avg_frame_rate', '0/1')))
                    except (ValueError, ZeroDivisionError):
                        raise ValueError('参考视频缺少有效帧率') from None
                if kind == 'video' and reference_request is not None:
                    with measure(record, 'transform_seconds'):
                        path, metadata, processing = await adapt_reference_video(
                            path, metadata, reference_request, self.settings.sd25_video_policy)
                if kind == 'audio' and silence_seconds:
                    with measure(record, 'transform_seconds'):
                        path, metadata, processing = await append_audio_silence(path, metadata, silence_seconds)
                if processing:
                    if metadata['byteSize'] > 150*1024*1024:
                        raise ValueError('参考素材转换后超过 150 MiB')
                    content = await file_io(path.read_bytes)
                    mime, filename = metadata['mimeType'], metadata['fileName']
            request={'fileName':filename,'fileType':mime,'expiresIn':86400}
            request.update({k:metadata[k] for k in ('width','height') if k in metadata})
            with measure(record, 'sign_upload_seconds'):
                signed=await self.rpc('uploadRouter.getPresignedUrl',request,post=True)
            target=public_media_url(signed['presignedUrl'])
            host=urlsplit(target).hostname
            if not host.endswith('.amazonaws.com') or not host.startswith('artlist-'):
                raise ValueError('上传签名地址不在 Artlist 存储域名内')
            # The upload client has no Artlist cookies or authorization headers.
            content = bytes(content)
            with measure(record, 'upload_seconds'):
                response=await http.put(target,content=content,headers={'Content-Type':mime},timeout=timeout)
            record['upload_bytes'] = len(content)
            if not 200<=response.status_code<300:
                raise ValueError('Artlist 参考素材上传失败')
            # The bucket is private: fileUrl is an unsigned object location and
            # presignedUrl above authorizes PUT only. Match the captured browser
            # flow by obtaining a separate GET signature after the upload.
            with measure(record, 'sign_read_seconds'):
                preview=await self.rpc('uploadRouter.getPresignedUrlFromKey',
                                       {'fileKey':signed['fileKey'],'expiresIn':86400},post=True)
            readable=public_media_url(preview.get('presignedUrl',''))
            location,uploaded=urlsplit(readable),urlsplit(target)
            query=parse_qs(location.query)
            if (location.scheme!='https' or location.netloc!=uploaded.netloc or location.path!=uploaded.path
                    or not query.get('X-Amz-Signature') or query.get('x-id')!=['GetObject']):
                raise GatewayError('Artlist 未返回对应素材的有效下载签名', 'media_signature_invalid', 422)
            # Verify access without Artlist session headers before any billable
            # generation; a signed GET must not be tested with unsigned HEAD.
            with measure(record, 'read_check_seconds'):
                async with http.stream('GET',readable,headers={'Range':'bytes=0-0'},timeout=timeout) as access:
                    if access.status_code not in {200,206}:
                        raise GatewayError(f'Artlist 上传素材不可读取（HTTP {access.status_code}）', 'media_unreachable', 422)
                    # Consume a genuine one-byte range so HTTP/1.1 can reuse its
                    # connection; never download the whole object for HTTP 200.
                    if access.status_code == 206:
                        received = 0
                        async for chunk in access.aiter_bytes():
                            received += len(chunk)
                            if received > 1:
                                break
            return {'file_key':signed['fileKey'],'file_url':readable,'metadata':metadata,
                    **({'processing': processing} if processing else {})}

    async def prepare(self, task):
        started = time.monotonic()
        timing = {}
        context = preparation_context.set(timing)
        try:
            return await self._prepare(task, timing)
        finally:
            preparation_context.reset(context)
            timing['total_seconds'] = round(time.monotonic()-started, 3)
            self.db.update_task(task['id'], result={**self.db.task(task['id'])['result'], 'preparation_timing': timing})

    @staticmethod
    async def parallel_uploads(uploads):
        # gather preserves input order. On failure/cancellation, drain siblings
        # before unwinding so no upload or FFmpeg survives its failed task.
        jobs = [asyncio.create_task(upload) for upload in uploads]
        try:
            return await asyncio.gather(*jobs)
        except BaseException:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            raise

    async def _prepare_media(self, task, timing):
        request=task['request']
        validate_request(request,task['profile'])
        _, tags = reference_prompt(request)
        if reference_header(request, tags):
            self.db.update_task(task['id'], result={**self.db.task(task['id'])['result'], 'prompt_processing': {
                'policy': 'reference_header', 'tags': [tag['tagId'] for tag in tags],
                'action': '前置已使用的引用标签，长编号优先映射；保留完整原文和素材序号',
            }})
        assets={}
        task_slots = asyncio.Semaphore(3)
        timing['media_parallelism'] = 3
        timing['media_account_parallelism'] = 12
        timing['media_items'] = []
        def save_processing():
            records = [{**asset['processing'], 'field': key, 'index': index+1}
                       for key, values in assets.items() for index, asset in enumerate(values) if asset and asset.get('processing')]
            if records:
                self.db.update_task(task['id'], result={**self.db.task(task['id'])['result'], 'media_processing': records})
        async def upload_reference(field, index, url, kind, options, record):
            context = media_context.set(record)
            try:
                with measure(record, 'task_queue_seconds'):
                    await task_slots.acquire()
                try:
                    assets[field][index] = await self.upload(url, kind, **options)
                finally:
                    task_slots.release()
            finally:
                record['total_seconds'] = round(record.get('total_seconds', 0) + record.get('task_queue_seconds', 0), 3)
                media_context.reset(context)
                save_processing()
        def schedule_upload(field, index, url, kind, options):
            record = {'field': field, 'index': index+1, 'pass': 'padding' if options.get('silence_seconds') else 'initial'}
            timing['media_items'].append(record)
            return upload_reference(field, index, url, kind, options, record)
        uploads = []
        for field,kind in [('image_urls','image'),('video_urls','video'),('audio_urls','audio'),('first_frame','image'),('last_frame','image')]:
            urls=request.get(field) or []
            if isinstance(urls,str):urls=[urls]
            options = {'reference_request': request} if field == 'video_urls' and task['profile']['group_id'] == 515 else {}
            assets[field] = [None] * len(urls)
            uploads.extend(schedule_upload(field, index, url, kind, options) for index, url in enumerate(urls))
        await self.parallel_uploads(uploads)
        if assets.get('audio_urls'):
            # Resolve actual per-file/total limits before padding; this quote is
            # never submitted. Only the final quote below may authorize a job.
            preliminary, *_ = await self.quote(request, assets, allow_short_audio=True)
            context = preliminary.get('modelContextConfig', {})
            plan = audio_silence_plan(assets['audio_urls'], context)
            if any(plan) and context.get('audioFormats') and 'WAV' not in context['audioFormats']:
                raise ValueError('Artlist 当前子模型不支持补齐后使用的 WAV 音频格式')
            uploads = []
            for index, seconds in enumerate(plan):
                if seconds:
                    # Read the exact uploaded object, not a potentially changing
                    # caller URL. Downloads/uploads keep this account's proxy.
                    uploads.append(schedule_upload('audio_urls', index,
                        assets['audio_urls'][index]['file_url'], 'audio', {'silence_seconds': seconds}))
            await self.parallel_uploads(uploads)
            self.validate_media(assets, context)
        return assets

    async def _prepare(self, task, timing):
        timing['stage'] = 'media'
        request = task['request']
        with measure(timing, 'media_seconds'):
            assets = await self._prepare_media(task, timing)
        # A cold browser verification may take tens of seconds. Obtain the
        # short-lived cost signature only after normal verification completes.
        timing['stage'] = 'verification'
        phase = time.monotonic()
        token=self.db.take_verification(task['id'])
        verification = {'token': token} if token else (await self.browsers.generation_verification(self.account_id) if self.browsers else {})
        timing['verification_seconds'] = round(time.monotonic()-phase, 3)
        if verification.get('timing'):
            timing['browser'] = verification['timing']
        timing['stage'] = 'quote'
        phase = time.monotonic()
        quote,inputs,settings,artifacts=await self.quote(request,assets)
        timing['quote_seconds'] = round(time.monotonic()-phase, 3)
        quoted_at = time.monotonic()
        timing['stage'] = 'eligibility_and_session'
        eligibility=await self.rpc('userGenerationRouter.checkGenerationEligibility',{'price':quote['cost'],'modelId':quote['modelId'],'settings':settings},post=True)
        self.db.update_credentials(self.account_id, {'web_last_preflight': {'eligibility': eligibility, 'model_id': quote['modelId']}})
        if eligibility.get('isFairUseExceeded') or eligibility.get('isConcurrencyExceeded'):
            raise GatewayError('Artlist 额度或并发不可用', 'generation_rejected', 422)
        # The captured web client supplies crypto.randomUUID() for each new
        # chat session. This required field is not an account membership lookup.
        session_input={'name':'ART2API '+task['id'],
                       'teamId':(self.secret().get('web_team_id') or '').strip() or str(uuid.uuid4())}
        session=await self.rpc('chatSession.createChatSession',session_input,post=True)
        if not session.get('id'):
            raise GatewayError('Artlist 未返回会话 ID', 'web_protocol_error')
        self.db.update_task(task['id'],result={**self.db.task(task['id'])['result'], 'chat_session_id':session['id'],'resolved_model_id':quote['modelId'], 'quote_age_seconds': round(time.monotonic()-quoted_at,3), 'verification': 'token' if verification.get('token') else 'client_error' if verification.get('client_error') else 'none'})
        payload = generation_payload(session['id'],quote,inputs,settings,artifacts,verification.get('token', ''))
        if verification.get('client_error'):
            payload['turnstileClientError'] = verification['client_error']
        timing['eligibility_and_session_seconds'] = round(time.monotonic()-quoted_at, 3)
        timing['stage'] = 'ready'
        return payload

    async def submit(self, payload):
        data=await self.rpc('userGenerationRouter.createUserGeneration',payload,post=True)
        if not isinstance(data,dict) or not data.get('id'):
            raise GatewayError('提交响应未包含生成任务 ID，不能自动重提', 'submission_unknown', ambiguous=True)
        self.db.update_account(self.account_id, last_error='')
        return str(data['id'])

    async def query(self, generation_id):
        data=await self.rpc('userGenerationRouter.getUserGenerationById',{'id':generation_id})
        if not isinstance(data,dict) or data.get('id')!=generation_id:
            raise GatewayError('查询返回的任务 ID 不匹配', 'web_protocol_error')
        result=generation_result(data)
        if result['status']=='succeeded':
            # Resolve output separately: output IDs are not generation IDs.
            if result.get('output_id'):
                output=await self.rpc('userGenerationRouter.getUserGenerationOutputById',{'id':result['output_id']})
                if output.get('generationId')!=generation_id or output.get('id')!=result['output_id']:
                    raise GatewayError('输出文件与生成任务不匹配', 'web_protocol_error')
                if not output.get('fileUrl'):
                    return {'status':'running', 'upstream_status':'output_pending'}
                result['video_url']=public_media_url(output['fileUrl'])
                result['metadata']=output.get('metadata') or result.get('metadata')
            else:
                result['video_url']=public_media_url(result['video_url'])
        return result
