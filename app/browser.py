from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from app.errors import GatewayError
from app.cookies import browser_cookies
from app.network import browser_proxy
from app.web import web_task_context


GENERATOR_URL = 'https://toolkit.artlist.io/image-video-generator?mode=video'
SDK_READY = "location.origin === 'https://toolkit.artlist.io' && location.pathname === '/image-video-generator' && !!window.turnstile"
CLEAR_VERIFICATION = """(()=>{
  delete window.__art2apiVerificationRun;
  try { if(window.__art2apiVerificationWidget!==undefined) window.turnstile?.remove(window.__art2apiVerificationWidget); }
  finally {
    document.getElementById('art2api-normal-verification')?.remove();
    delete window.__art2apiVerification;
    delete window.__art2apiVerificationWidget;
  }
  return true;
})()"""


class CDP:
    """JSON-RPC over Chromium's native DevTools WebSocket."""
    def __init__(self, websocket):
        self.ws, self.serial, self.pending = websocket, 0, {}
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                future = self.pending.get(message.get('id'))
                if future and not future.done():
                    if 'error' in message:
                        future.set_exception(GatewayError('浏览器 CDP 指令失败', 'browser_error'))
                    else:
                        future.set_result(message.get('result', {}))
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(GatewayError('浏览器连接已断开', 'browser_error'))

    async def call(self, method, params=None):
        self.serial += 1
        identifier = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.ws.send(json.dumps({'id': identifier, 'method': method, 'params': params or {}}))
            return await asyncio.wait_for(future, 45)
        finally:
            self.pending.pop(identifier, None)

    async def evaluate(self, expression):
        result = await self.call('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if result.get('exceptionDetails'):
            raise GatewayError('浏览器页面操作失败', 'browser_error')
        return result.get('result', {}).get('value')

    async def close(self):
        await self.ws.close()
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)


class BrowserManager:
    """An isolated Chromium process, profile and mandatory proxy for each account."""
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.verification_locks = {}

    def account_lock(self, account_id):
        # Verification, operator actions and lifecycle changes share ownership.
        return self.verification_locks.setdefault(account_id, asyncio.Lock())

    @staticmethod
    def live(session):
        return bool(session and session['process'].returncode is None
                    and session.get('cdp') and not session['cdp'].reader.done())

    async def verification_page(self, account_id, account):
        """Called with the account lock held; rebuild only an unusable session."""
        session = self.sessions.get(account_id)
        reused = bool(self.live(session) and session.get('purpose') == 'verification'
                      and session.get('proxy_version') == account['proxy_version']
                      and session.get('web_user_id') == account['credentials'].get('web_user_id')
                      and session.get('headless') == self.settings.browser_headless)
        page_ready = False
        if reused:
            try:
                page_ready = await asyncio.wait_for(session['cdp'].evaluate(SDK_READY), 5)
            except Exception:
                reused = False
        if not reused:
            await self._open(account_id, 'about:blank', purpose='verification')
            session = self.sessions[account_id]
        cdp = session['cdp']
        session['expires'] = time.time()+self.settings.browser_timeout
        await cdp.call('Network.enable')
        # Apply before the first toolkit navigation, and retain it on warm pages.
        await cdp.call('Network.setBlockedURLs', {'urls': ['*createUserGeneration*']})
        cookie = account['credentials'].get('web_cookie', '')
        if session.get('synced_cookie') != cookie:
            await cdp.call('Network.clearBrowserCookies')
            await cdp.call('Network.setCookies', {'cookies': browser_cookies(cookie)})
            session['synced_cookie'] = cookie
        if not page_ready:
            await cdp.call('Page.navigate', {'url': GENERATOR_URL})
            for _ in range(60):
                if await cdp.evaluate(SDK_READY):
                    break
                await asyncio.sleep(1)
            else:
                raise GatewayError('后台网页验证脚本未就绪，请检查账号代理或登录', 'verification_unavailable', 503)
        return cdp, {'browser_reused': reused, 'page_reused': bool(page_ready)}

    @staticmethod
    def profile_guard(profile):
        """Serialize profile ownership and clear locks left by a stopped container."""
        if os.name == 'nt':
            return None
        import fcntl
        guard = (profile / '.art2api.lock').open('a')
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            chrome_lock = profile / 'SingletonLock'
            if chrome_lock.is_symlink():
                host, _, pid = os.readlink(chrome_lock).rpartition('-')
                if host == socket.gethostname() and pid.isdigit() and Path('/proc', pid).exists():
                    raise GatewayError('该账号浏览器 Profile 正被使用', 'browser_error')
                # Only Chromium's three known singleton links, inside this
                # exclusively owned profile. Never delete profile contents.
                for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                    (profile / name).unlink(missing_ok=True)
            return guard
        except Exception:
            guard.close()
            raise

    async def generation_verification(self, account_id):
        """Run the normal SDK; never create generations or solve challenges."""
        started = time.monotonic()
        async with self.account_lock(account_id):
            timing = {'queue_seconds': round(time.monotonic()-started, 3)}
            account = self.db.account(account_id, True)
            secret = account['credentials']
            cdp = None
            try:
                phase = time.monotonic()
                cdp, reuse = await self.verification_page(account_id, account)
                timing.update(reuse, browser_setup_seconds=round(time.monotonic()-phase, 3))
                phase = time.monotonic()
                identity = await cdp.evaluate("fetch('/api/auth/session',{credentials:'include',cache:'no-store'}).then(r=>r.json()).then(s=>s.user?.id||null)")
                timing['session_check_seconds'] = round(time.monotonic()-phase, 3)
                if not identity or identity != secret.get('web_user_id'):
                    raise GatewayError('后台浏览器未保持原账号登录；已保留原会话，请重新导入登录', 'reauthorization_required', 401)
                # Each warm verification gets a new widget and callback scope.
                phase = time.monotonic()
                await cdp.evaluate(CLEAR_VERIFICATION)
                await cdp.evaluate(Path(__file__).with_name('verification.js').read_text(encoding='utf-8'))
                for _ in range(60):
                    value = await cdp.evaluate('window.__art2apiVerification')
                    if value and value.get('status') in {'ready', 'error', 'unsupported', 'timeout'}:
                        break
                    await asyncio.sleep(1)
                else:
                    raise GatewayError('Artlist 需要在账号控制台完成正常网页验证', 'verification_required', 503)
                timing['sdk_seconds'] = round(time.monotonic()-phase, 3)
                if value['status'] == 'ready':
                    result = {'token': value['token']}
                else:
                    # Pass the real SDK result as the first-party tRPC link does.
                    # Artlist decides whether this request is allowed.
                    result = {'client_error': value['code']}
                credentials = await self._session_credentials(cdp)
                current = self.db.account(account_id, True)
                if (current['proxy_version'] != account['proxy_version']
                        or current['credentials'].get('web_user_id') != secret.get('web_user_id')):
                    raise GatewayError('验证期间账号或代理已变更', 'proxy_binding_changed')
                self.db.update_web_session_if_current(account_id, secret, credentials)
                self.sessions[account_id]['synced_cookie'] = credentials['cookie']
                timing['total_seconds'] = round(time.monotonic()-started, 3)
                self.db.event('web_verification', json.dumps({
                    'result': 'token_ready' if 'token' in result else result['client_error'],
                    'browser_pid': self.sessions[account_id]['process'].pid, **timing,
                }), account_id, web_task_context.get())
                return {**result, 'timing': timing}
            except BaseException:
                # An interrupted/invalid page is not reused by the next task.
                await self._close(account_id)
                raise
            finally:
                if account_id in self.sessions:
                    try:
                        if cdp:
                            await cdp.evaluate(CLEAR_VERIFICATION)
                    except Exception:
                        await self._close(account_id)
                    else:
                        self.sessions[account_id]['expires'] = time.time()+self.settings.browser_timeout

    async def open(self, account_id, authorization_url):
        async with self.account_lock(account_id):
            await self._open(account_id, authorization_url)

    async def _open(self, account_id, authorization_url, *, purpose='operator'):
        async with self.lock:
            await self._close(account_id)
            account = self.db.account(account_id, True)
            if account['active_tasks'] and account['backend'] != 'web':
                raise ValueError('存在未结束任务，不能重新登录')
            proxy = browser_proxy(account['credentials']['proxy_url'])
            if proxy.get('username'):
                raise ValueError('CDP 浏览器需要无认证本地代理，例如 Xray；协议调用仍支持认证代理')
            executable = self.settings.chrome_executable or shutil.which('chromium') or shutil.which('google-chrome')
            if not executable:
                raise GatewayError('未配置 Chromium 可执行文件', 'browser_error')
            profile = self.settings.data_dir / 'browser-profiles' / account_id
            profile.mkdir(parents=True, exist_ok=True)
            port_file = profile / 'DevToolsActivePort'
            # Use a concrete native CDP port, as in the operator's Chrome launch.
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            args = [executable, '--remote-debugging-address=127.0.0.1', '--remote-debugging-port='+str(port),
                    '--user-data-dir='+str(profile.resolve()), '--proxy-server='+proxy['server'],
                    '--proxy-bypass-list=<-loopback>', '--disable-quic', '--disable-dev-shm-usage',
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp', '--window-size=1100,760', 'about:blank']
            if os.name != 'nt' and hasattr(os, 'geteuid') and os.geteuid() == 0:
                args.insert(1, '--no-sandbox')
            options = {}
            display = None
            if self.settings.browser_headless:
                args.insert(1, '--headless=new')
            if os.name == 'nt':
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = subprocess.SW_HIDE
                options['startupinfo'] = startup
            guard = self.profile_guard(profile)
            try:
                port_file.unlink(missing_ok=True)
                if os.name != 'nt' and not self.settings.browser_headless:
                    xvfb = shutil.which('Xvfb')
                    if not xvfb:
                        raise GatewayError('后台图形浏览器需要 Xvfb；请使用项目 Docker 镜像', 'browser_error')
                    display = await asyncio.create_subprocess_exec(xvfb, '-displayfd', '1', '-screen', '0', '1100x760x24', '-nolisten', 'tcp', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    number = (await asyncio.wait_for(display.stdout.readline(), 10)).decode().strip()
                    if not number.isdigit():
                        raise GatewayError('后台浏览器显示服务启动失败', 'browser_error')
                    options['env'] = {**os.environ, 'DISPLAY': ':'+number}
                process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, **options)
            except BaseException:
                if display and display.returncode is None:
                    display.terminate()
                    await display.wait()
                if guard:
                    guard.close()
                raise
            self.sessions[account_id] = {
                'process': process, 'expires': time.time()+self.settings.browser_timeout,
                'cdp': None, 'profile_guard': guard, 'display': display, 'purpose': purpose,
                'proxy_version': account['proxy_version'],
                'web_user_id': account['credentials'].get('web_user_id'),
                'headless': self.settings.browser_headless,
            }
            try:
                # Only localhost CDP traffic bypasses the account proxy.
                async with httpx.AsyncClient(trust_env=False, timeout=1) as http:
                    for _ in range(100):
                        if process.returncode is not None:
                            raise GatewayError('账号浏览器启动失败', 'browser_error')
                        try:
                            response = await http.get(f'http://127.0.0.1:{port}/json/list')
                            response.raise_for_status()
                            page = next((p for p in response.json() if p.get('type') == 'page'), None)
                            if page:
                                break
                        except (httpx.HTTPError, ValueError):
                            pass
                        await asyncio.sleep(.2)
                    else:
                        raise GatewayError('账号浏览器 CDP 页面启动超时', 'browser_error')
                port_file.write_text(str(port)+'\n')
                cdp = CDP(await connect(page['webSocketDebuggerUrl'], proxy=None, max_size=20_000_000))
                self.sessions[account_id].update(cdp=cdp, port=port, target=page['id'])
                await cdp.call('Page.enable')
                await cdp.call('Emulation.setDeviceMetricsOverride', {'width':1100,'height':760,'deviceScaleFactor':1,'mobile':False})
                await cdp.call('Page.navigate', {'url': authorization_url})
            except BaseException:
                await self._close(account_id)
                raise

    async def page(self, account_id):
        session = self.sessions.get(account_id)
        if not self.live(session):
            raise ValueError('浏览器未启动或已超时，请重新连接')
        session['expires'] = time.time()+self.settings.browser_timeout
        return session['cdp']

    async def snapshot(self, account_id):
        async with self.account_lock(account_id):
            cdp = await self.page(account_id)
            result = await cdp.call('Page.captureScreenshot', {'format':'jpeg','quality':75,'captureBeyondViewport':False})
            return base64.b64decode(result['data'])

    async def session_credentials(self, account_id):
        async with self.account_lock(account_id):
            return await self._session_credentials(await self.page(account_id))

    async def _session_credentials(self, cdp):
        result = await cdp.call('Network.getCookies', {'urls':['https://toolkit.artlist.io/']})
        agent = await cdp.evaluate('navigator.userAgent')
        return {'cookie': '; '.join(c['name']+'='+c['value'] for c in result['cookies']), 'user_agent': agent}

    async def action(self, account_id, action):
        async with self.account_lock(account_id):
            await self._action(account_id, action)

    async def _action(self, account_id, action):
        cdp = await self.page(account_id)
        kind = action['kind']
        if kind == 'click':
            for event in ('mousePressed','mouseReleased'):
                await cdp.call('Input.dispatchMouseEvent', {'type':event,'x':action['x'],'y':action['y'],'button':'left','clickCount':1})
        elif kind == 'type':
            await cdp.call('Input.insertText', {'text':str(action['text'])})
        elif kind == 'scroll':
            await cdp.call('Input.dispatchMouseEvent', {'type':'mouseWheel','x':550,'y':380,'deltaX':0,'deltaY':action.get('y',0)})
        elif kind == 'key':
            keys={'Tab':9,'Enter':13,'Backspace':8,'Escape':27,'ArrowDown':40,'ArrowUp':38,'ControlOrMeta+A':65}
            key=action['key']
            if key not in keys:
                raise ValueError('unsupported key')
            for event in ('keyDown','keyUp'):
                params={'type':event,'key':'a' if key=='ControlOrMeta+A' else key,'windowsVirtualKeyCode':keys[key],'modifiers':2 if key=='ControlOrMeta+A' else 0}
                if key=='Enter' and event=='keyDown':params['text']='\r'
                await cdp.call('Input.dispatchKeyEvent',params)
        else:
            raise ValueError('unsupported browser action')

    async def close(self, account_id):
        async with self.account_lock(account_id):
            await self._close(account_id)

    async def _close(self, account_id):
        session = self.sessions.pop(account_id, None)
        if not session:
            return
        try:
            if session.get('cdp'):
                try:
                    await asyncio.wait_for(session['cdp'].call('Browser.close'), 3)
                except Exception:
                    pass  # A successful Browser.close may close the socket first.
                await session['cdp'].close()
        finally:
            process = session['process']
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            if session.get('profile_guard'):
                session['profile_guard'].close()
            display = session.get('display')
            if display and display.returncode is None:
                display.terminate()
                await display.wait()

    async def cleanup(self):
        for account_id, session in list(self.sessions.items()):
            lock = self.account_lock(account_id)
            if lock.locked():
                continue
            if session.get('purpose') == 'verification':
                try:
                    account = self.db.account(account_id)
                    expired = not account['enabled'] and not account['active_tasks']
                except KeyError:
                    expired = True
                expired = expired or not self.live(session)
            else:
                expired = session['expires'] < time.time()
            if expired:
                async with lock:
                    if self.sessions.get(account_id) is session:
                        await self._close(account_id)

    async def stop(self):
        for account_id in list(self.sessions):
            await self.close(account_id)
