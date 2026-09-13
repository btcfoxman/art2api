from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from app.network import browser_proxy


class BrowserManager:
    """Account-isolated authorization browsers. All browser traffic uses the bound proxy."""
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.playwright = None
        self.sessions = {}
        self.lock = asyncio.Lock()

    async def open(self, account_id, authorization_url):
        async with self.lock:
            await self.close(account_id)
            account = self.db.account(account_id, True)
            if not self.playwright:
                self.playwright = await async_playwright().start()
            profile = self.settings.data_dir / 'browser-profiles' / account_id
            profile.mkdir(parents=True, exist_ok=True)
            context = await self.playwright.chromium.launch_persistent_context(
                str(profile), executable_path=self.settings.chrome_executable or None,
                headless=True, proxy=browser_proxy(account['credentials']['proxy_url']),
                viewport={'width': 1100, 'height': 760},
                args=['--disable-dev-shm-usage', '--disable-quic', '--proxy-bypass-list=<-loopback>',
                      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
            )
            page = context.pages[0] if context.pages else await context.new_page()
            self.sessions[account_id] = {'context': context, 'page': page, 'expires': time.time() + self.settings.browser_timeout}
            try:
                await page.goto(authorization_url, wait_until='domcontentloaded', timeout=60000)
            except Exception:
                await self.close(account_id)
                raise

    def page(self, account_id):
        session = self.sessions.get(account_id)
        if not session or time.time() > session['expires']:
            raise ValueError('授权浏览器未启动或已超时，请重新连接')
        pages = [p for p in session['context'].pages if not p.is_closed()]
        if pages:
            session['page'] = pages[-1]
        return session['page']

    async def snapshot(self, account_id):
        page = self.page(account_id)
        return await page.screenshot(type='jpeg', quality=75)

    async def action(self, account_id, action):
        page = self.page(account_id)
        kind = action['kind']
        if kind == 'click':
            await page.mouse.click(float(action['x']), float(action['y']))
        elif kind == 'type':
            await page.keyboard.insert_text(str(action['text']))
        elif kind == 'key':
            if action['key'] not in {'Tab', 'Enter', 'Backspace', 'Escape', 'ArrowDown', 'ArrowUp', 'ControlOrMeta+A'}:
                raise ValueError('unsupported key')
            await page.keyboard.press(action['key'])
        elif kind == 'scroll':
            await page.mouse.wheel(0, max(-760, min(760, float(action.get('y', 0)))))
        else:
            raise ValueError('unsupported browser action')

    async def close(self, account_id):
        session = self.sessions.pop(account_id, None)
        if session:
            await session['context'].close()

    async def cleanup(self):
        for account_id, session in list(self.sessions.items()):
            if session['expires'] < time.time():
                await self.close(account_id)

    async def stop(self):
        for account_id in list(self.sessions):
            await self.close(account_id)
        if self.playwright:
            await self.playwright.stop()

