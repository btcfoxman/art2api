from unittest.mock import AsyncMock, Mock
import asyncio
import json
import os
import socket
import time

import pytest

from app.browser import BrowserManager, CLEAR_VERIFICATION, SDK_READY
from app.config import Settings
from app.errors import GatewayError


class FakeCDP:
    def __init__(self, identity='user-1', sdk_result=None):
        self.identity, self.sdk_result = identity, sdk_result
        self.ready, self.rounds, self.token = False, 0, None
        self.reader = Mock()
        self.reader.done.return_value = False
        self.call = AsyncMock(side_effect=self.command)
        self.evaluate = AsyncMock(side_effect=self.expression)
        self.sdk_entered, self.sdk_gate = asyncio.Event(), None

    async def command(self, method, params=None):
        if method == 'Page.navigate': self.ready = True
        if method == 'Network.getCookies': return {'cookies': [{'name':'session','value':'rotated'}]}
        return {}

    async def expression(self, expression):
        if expression == SDK_READY: return self.ready
        if "fetch('/api/auth/session'" in expression: return self.identity
        if expression == CLEAR_VERIFICATION: self.token = None; return True
        if 'window.turnstile.render' in expression:
            self.rounds += 1
            self.token = 'normal-once-'+str(self.rounds)
            return True
        if expression == 'window.__art2apiVerification':
            self.sdk_entered.set()
            if self.sdk_gate: await self.sdk_gate.wait()
            return self.sdk_result or {'status':'ready','token':self.token}
        if expression == 'navigator.userAgent': return 'Browser'
        raise AssertionError('Unexpected CDP expression')


def fake_manager(cdp=None):
    db = Mock()
    db.account.return_value = {'proxy_version':1,'enabled':True,'active_tasks':0,
                              'credentials':{'web_cookie':'session=private','web_user_id':'user-1'}}
    manager = BrowserManager(db, Settings())
    cdp = cdp or FakeCDP()
    async def start(aid, url, *, purpose='operator'):
        await manager._close(aid)
        manager.sessions[aid] = {'cdp':cdp,'process':Mock(returncode=None,pid=123),
                                'expires':time.time()+900,'purpose':purpose,'headless':False,
                                'proxy_version':db.account.return_value['proxy_version'],
                                'web_user_id':db.account.return_value['credentials']['web_user_id']}
    async def close(aid): manager.sessions.pop(aid, None)
    manager._open = AsyncMock(side_effect=start)
    manager._close = AsyncMock(side_effect=close)
    return manager, db, cdp


@pytest.mark.asyncio
@pytest.mark.parametrize('sdk_result,expected', [({'status':'ready','token':'normal-once'}, {'token':'normal-once'}), ({'status':'error','code':'600010'}, {'client_error':'600010'})])
async def test_native_verification_blocks_paid_browser_request_and_forwards_actual_callback(sdk_result, expected):
    manager, db, cdp = fake_manager(FakeCDP(sdk_result=sdk_result))
    result = await manager.generation_verification('account-1')
    assert {k:v for k,v in result.items() if k!='timing'} == expected
    assert result['timing']['browser_reused'] is False
    manager._open.assert_awaited_once_with('account-1','about:blank',purpose='verification')
    calls = cdp.call.await_args_list
    blocked = next(i for i,c in enumerate(calls) if c.args[0]=='Network.setBlockedURLs')
    navigation = next(i for i,c in enumerate(calls) if c.args[0]=='Page.navigate')
    assert blocked < navigation
    assert calls[blocked].args[1]['urls'] == ['*createUserGeneration*']
    assert all(not c.args[0].startswith('Input.') for c in calls)
    assert cdp.token is None
    assert 'normal-once' not in str(db.event.call_args_list)


@pytest.mark.skipif(os.name == 'nt', reason='Chromium singleton symlinks are Linux-specific')
def test_stopped_container_profile_lock_is_removed_under_exclusive_guard(tmp_path):
    (tmp_path/'SingletonLock').symlink_to('previous-container-123')
    (tmp_path/'SingletonSocket').symlink_to('/tmp/previous-browser/socket')
    (tmp_path/'SingletonCookie').symlink_to('12345')
    (tmp_path/'Cookies').write_text('preserve private profile')
    guard=BrowserManager.profile_guard(tmp_path)
    try:
        assert not (tmp_path/'SingletonLock').is_symlink()
        assert (tmp_path/'Cookies').read_text()=='preserve private profile'
        with pytest.raises(BlockingIOError):BrowserManager.profile_guard(tmp_path)
    finally:guard.close()
    (tmp_path/'SingletonLock').symlink_to(f'{socket.gethostname()}-{os.getpid()}')
    with pytest.raises(Exception,match='Profile'):BrowserManager.profile_guard(tmp_path)
    assert (tmp_path/'SingletonLock').is_symlink()


@pytest.mark.asyncio
async def test_browser_close_flushes_profile_before_releasing_ownership():
    events=[]
    class CDP:
        async def call(self, method):events.append(method)
        async def close(self):events.append('socket_closed')
    class Process:
        returncode=None
        async def wait(self):events.append('process_exited');self.returncode=0
        def terminate(self):raise AssertionError('Normal close must flush profile gracefully')
    class Guard:
        def close(self):events.append('profile_released')
    manager=BrowserManager(None, Settings())
    manager.sessions['a']={'cdp':CDP(),'process':Process(),'profile_guard':Guard()}
    await manager.close('a')
    assert events == ['Browser.close','socket_closed','process_exited','profile_released']


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [None, 'different-account'])
async def test_browser_never_overwrites_login_with_anonymous_or_other_account(identity):
    manager, db, cdp = fake_manager(FakeCDP(identity=identity))
    with pytest.raises(GatewayError) as error:
        await manager.generation_verification('a')
    assert error.value.code=='reauthorization_required'
    db.update_web_session_if_current.assert_not_called()
    assert cdp.evaluate.await_count==2
    assert 'a' not in manager.sessions


@pytest.mark.asyncio
async def test_consecutive_tasks_reuse_process_page_but_receive_fresh_tokens():
    manager, db, cdp = fake_manager()
    first = await manager.generation_verification('a')
    process = manager.sessions['a']['process']
    # Background verification persists beyond the operator-window idle timeout.
    manager.sessions['a']['expires'] = 0
    await manager.cleanup()
    second = await manager.generation_verification('a')
    assert manager.sessions['a']['process'] is process
    manager._open.assert_awaited_once()
    assert first['token'] != second['token']
    assert second['timing']['browser_reused'] and second['timing']['page_reused']
    assert sum(c.args[0]=='Page.navigate' for c in cdp.call.await_args_list) == 1
    assert cdp.rounds == 2 and cdp.token is None
    assert json.loads(db.event.call_args.args[1])['browser_pid'] == 123


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['process_exit','cdp_closed','proxy','identity','headless','cdp_timeout'])
async def test_unusable_browser_is_rebuilt_before_new_verification(change):
    manager, db, cdp = fake_manager()
    await manager.generation_verification('a')
    if change == 'process_exit': manager.sessions['a']['process'].returncode = 1
    if change == 'cdp_closed': cdp.reader.done.return_value = True
    if change == 'proxy': db.account.return_value['proxy_version'] += 1
    if change == 'identity':
        db.account.return_value['credentials']['web_user_id'] = cdp.identity = 'user-2'
    if change == 'headless': manager.settings.browser_headless = True
    if change == 'cdp_timeout':
        original = cdp.expression
        async def stale(expression):
            if expression == SDK_READY:
                cdp.evaluate.side_effect = original
                raise TimeoutError()
            return await original(expression)
        cdp.evaluate.side_effect = stale
    result = await manager.generation_verification('a')
    assert manager._open.await_count == 2
    assert result['timing']['browser_reused'] is False


@pytest.mark.asyncio
async def test_page_navigation_recovers_without_restarting_healthy_process():
    manager, db, cdp = fake_manager()
    await manager.generation_verification('a')
    cdp.ready = False
    result = await manager.generation_verification('a')
    manager._open.assert_awaited_once()
    assert result['timing']['browser_reused'] and not result['timing']['page_reused']


@pytest.mark.asyncio
async def test_account_verifications_are_serial_and_cleanup_does_not_interrupt():
    manager, db, cdp = fake_manager()
    cdp.sdk_gate = asyncio.Event()
    first = asyncio.create_task(manager.generation_verification('a'))
    await cdp.sdk_entered.wait()
    second = asyncio.create_task(manager.generation_verification('a'))
    manager.sessions['a']['expires'] = 0
    db.account.return_value.update(enabled=False, active_tasks=0)
    await manager.cleanup()
    assert 'a' in manager.sessions and cdp.rounds == 1
    cdp.sdk_gate.set()
    results = await asyncio.gather(first, second)
    assert results[0]['token'] != results[1]['token']
    manager._open.assert_awaited_once()
    await manager.cleanup()
    assert 'a' not in manager.sessions


@pytest.mark.asyncio
async def test_manual_close_waits_for_verification_ownership():
    manager, db, cdp = fake_manager()
    cdp.sdk_gate = asyncio.Event()
    verify = asyncio.create_task(manager.generation_verification('a'))
    await cdp.sdk_entered.wait()
    close = asyncio.create_task(manager.close('a'))
    await asyncio.sleep(0)
    assert not close.done() and 'a' in manager.sessions
    cdp.sdk_gate.set()
    await asyncio.gather(verify, close)
    assert 'a' not in manager.sessions


@pytest.mark.asyncio
async def test_cancelled_verification_discards_page_and_releases_account_lock():
    manager, db, cdp = fake_manager()
    cdp.sdk_gate = asyncio.Event()
    verify = asyncio.create_task(manager.generation_verification('a'))
    await cdp.sdk_entered.wait()
    verify.cancel()
    with pytest.raises(asyncio.CancelledError): await verify
    assert not manager.account_lock('a').locked() and 'a' not in manager.sessions


@pytest.mark.asyncio
async def test_cookie_changes_are_synced_without_reloading_warm_page():
    manager, db, cdp = fake_manager()
    await manager.generation_verification('a')
    db.account.return_value['credentials']['web_cookie'] = 'session=new-login'
    await manager.generation_verification('a')
    cookies = [c.args[1]['cookies'] for c in cdp.call.await_args_list if c.args[0]=='Network.setCookies']
    assert cookies[-1][0]['value'] == 'new-login'
    assert sum(c.args[0]=='Page.navigate' for c in cdp.call.await_args_list) == 1


def test_browser_cookie_compare_and_swap_preserves_newer_login(tmp_path):
    from cryptography.fernet import Fernet
    from app.db import Database
    db = Database(tmp_path/'cookies.db', Fernet.generate_key().decode())
    aid = db.save_account({'name':'web','proxy_url':'socks5://xray:20001','backend':'web'})['id']
    db.update_credentials(aid, {'web_cookie':'session=before','web_user_id':'user-1'})
    original = db.account(aid, True)['credentials']
    assert db.update_web_session_if_current(aid, original, {'cookie':'session=browser','user_agent':'Browser'})
    assert db.account(aid, True)['credentials']['web_cookie'] == 'session=browser'
    original = db.account(aid, True)['credentials']
    db.update_credentials(aid, {'web_cookie':'session=newer-http-rotation'})
    assert not db.update_web_session_if_current(aid, original, {'cookie':'session=stale-browser','user_agent':'Browser'})
    assert db.account(aid, True)['credentials']['web_cookie'] == 'session=newer-http-rotation'
    db.close()


def test_late_sdk_callback_cannot_supply_a_token_to_the_next_task():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if not node: pytest.skip('Node is needed to execute the real verification callbacks')
    script = Path(__file__).parents[1].joinpath('app/verification.js').read_text(encoding='utf-8')
    harness = '''const vm = require('node:vm'), assert = require('node:assert/strict');
      const callbacks=[];
      const context=vm.createContext({window:{turnstile:{render:(_,config)=>{callbacks.push(config.callback);return callbacks.length;},remove:()=>{}}},document:{createElement:()=>({}),body:{append:()=>{}},getElementById:()=>({remove:()=>{}})}});
      vm.runInContext(SCRIPT,context); callbacks[0]('first-token');
      assert.equal(context.window.__art2apiVerification.token,'first-token');
      vm.runInContext(CLEANUP,context); callbacks[0]('stale-token');
      assert.equal(context.window.__art2apiVerification,undefined);
      vm.runInContext(SCRIPT,context); callbacks[0]('late-old-token');
      assert.equal(context.window.__art2apiVerification.status,'waiting');
      callbacks[1]('second-token'); assert.equal(context.window.__art2apiVerification.token,'second-token');
      vm.runInContext(CLEANUP,context); assert.equal(context.window.__art2apiVerification,undefined);
    '''.replace('SCRIPT', json.dumps(script)).replace('CLEANUP', json.dumps(CLEAR_VERIFICATION))
    subprocess.run([node,'-e',harness],check=True,capture_output=True,text=True)


@pytest.mark.asyncio
async def test_cleanup_keeps_disabled_account_until_its_tasks_finish():
    manager, db, cdp = fake_manager()
    await manager.generation_verification('a')
    db.account.return_value.update(enabled=False, active_tasks=1)
    await manager.cleanup()
    assert 'a' in manager.sessions
    db.account.return_value['active_tasks'] = 0
    await manager.cleanup()
    assert 'a' not in manager.sessions
