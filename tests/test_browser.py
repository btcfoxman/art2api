from unittest.mock import AsyncMock

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
