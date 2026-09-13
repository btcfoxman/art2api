from unittest.mock import AsyncMock
import os
import socket

import pytest

from app.browser import BrowserManager
from app.config import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize('sdk_result,expected', [({'status':'ready','token':'normal-once'}, {'token':'normal-once'}), ({'status':'error','code':'600010'}, {'client_error':'600010'})])
async def test_native_verification_blocks_paid_browser_request_and_forwards_actual_callback(sdk_result, expected):
    class DB:
        def account(self,*args):
            return {'proxy_version':1,'credentials':{'web_cookie':'session=private'}}
        def update_credentials(self,*args):pass
        def event(self,*args):pass
    manager = BrowserManager(DB(), Settings())
    cdp = AsyncMock()
    cdp.evaluate.side_effect = [True, True, sdk_result, True]
    manager.open = AsyncMock()
    manager.page = AsyncMock(return_value=cdp)
    manager.session_credentials = AsyncMock(return_value={'cookie':'session=rotated','user_agent':'Browser'})
    assert await manager.generation_verification('account-1') == expected
    manager.open.assert_awaited_once_with('account-1','about:blank')
    calls = cdp.call.await_args_list
    blocked = next(i for i,c in enumerate(calls) if c.args[0]=='Network.setBlockedURLs')
    navigation = next(i for i,c in enumerate(calls) if c.args[0]=='Page.navigate')
    assert blocked < navigation
    assert calls[blocked].args[1]['urls'] == ['*createUserGeneration*']
    assert all(not c.args[0].startswith('Input.') for c in calls)


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
