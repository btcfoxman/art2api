from __future__ import annotations

import asyncio
import json
import time

import httpx

from app.errors import GatewayError
from app.network import client


def unwrap(result):
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    texts = [item.get('text', '') for item in result.get('content', []) if item.get('type') == 'text']
    for text in texts:
        cleaned = text.strip()
        if cleaned.startswith('```'):
            cleaned = '\n'.join(cleaned.splitlines()[1:-1])
        try:
            value = json.loads(cleaned)
            if isinstance(value, (dict, list)):
                return value
        except ValueError:
            pass
    return {'text': '\n'.join(texts), 'content': result.get('content', [])}


class MCPClient:
    def __init__(self, account_id, db, oauth, settings):
        self.account_id, self.db, self.oauth, self.settings = account_id, db, oauth, settings
        self.session_id = None
        self.protocol = '2025-11-25'
        self.initialized = False
        self.counter = 0
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def rpc(self, method, params=None, *, mutating=False, notification=False):
        token = await self.oauth.access_token(self.account_id)
        account = self.db.account(self.account_id, True)
        self.counter += 1
        request_id = self.counter
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        if not notification:
            payload['id'] = request_id
        headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': self.protocol, 'User-Agent': 'art2api/0.1.0'}
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        # Account-local pacing also includes tools/list and initialization.
        await asyncio.sleep(max(0, self.last_call + .6 - time.monotonic()))
        self.last_call = time.monotonic()
        try:
            async with client(account['credentials']['proxy_url'], self.settings.request_timeout) as http:
                async with http.stream('POST', self.settings.mcp_url, json=payload, headers=headers) as response:
                    if response.status_code == 401:
                        raise GatewayError('Artlist authorization expired; reconnect or refresh the account', 'reauthorization_required', 401)
                    if response.status_code == 429:
                        raise GatewayError('Artlist request rate limit reached', 'rate_limited', 429, retryable=not mutating, ambiguous=mutating)
                    if response.status_code >= 400:
                        if response.status_code == 404:
                            self.initialized, self.session_id = False, None
                        raise GatewayError(f'Artlist MCP returned HTTP {response.status_code}', 'mcp_http_error', 502,
                                           ambiguous=mutating, retryable=not mutating and response.status_code >= 500)
                    if method == 'initialize':
                        self.session_id = response.headers.get('Mcp-Session-Id')
                    if notification:
                        return {}
                    if 'text/event-stream' in response.headers.get('content-type', ''):
                        data, total = [], 0
                        async for line in response.aiter_lines():
                            total += len(line)
                            if total > 8 * 1024 * 1024:
                                raise GatewayError('MCP response exceeds size limit', 'mcp_protocol_error', ambiguous=mutating)
                            if line.startswith('data:'):
                                data.append(line[5:].lstrip())
                            elif not line and data:
                                message = json.loads('\n'.join(data))
                                data = []
                                if message.get('id') == request_id:
                                    return self.result(message, mutating)
                        raise GatewayError('MCP stream ended without the requested response', 'mcp_protocol_error', ambiguous=mutating)
                    raw = await response.aread()
                    if len(raw) > 8 * 1024 * 1024:
                        raise ValueError('MCP response too large')
                    message = json.loads(raw)
                    if not isinstance(message, dict) or message.get('id') != request_id:
                        raise ValueError('MCP response ID mismatch')
                    return self.result(message, mutating)
        except (httpx.TransportError, ValueError) as exc:
            raise GatewayError('Artlist MCP proxy transport or response failed', 'submission_unknown' if mutating else 'mcp_transport_error',
                               ambiguous=mutating, retryable=not mutating) from exc

    @staticmethod
    def result(message, mutating):
        if 'error' in message:
            error = message['error']
            code = error.get('code') if isinstance(error, dict) else None
            raise GatewayError('Artlist MCP rejected the tool request', 'mcp_protocol_error',
                               ambiguous=mutating and code not in {-32600, -32601, -32602})
        result = message.get('result')
        if not isinstance(result, dict):
            raise GatewayError('Artlist MCP returned an invalid result', 'mcp_protocol_error', ambiguous=mutating)
        if result.get('isError'):
            data = unwrap(result)
            detail = json.dumps(data, ensure_ascii=False).lower()
            # A tool error alone does not prove a generation was never accepted.
            rejected = any(marker in detail for marker in ('insufficient credits', 'insufficient balance', 'unsupported model', 'invalid parameter', 'validation error'))
            raise GatewayError('Artlist tool reported a generation rejection' if rejected else 'Artlist tool reported an error',
                               'generation_rejected' if rejected else 'mcp_tool_error', ambiguous=mutating and not rejected)
        return result

    async def initialize(self):
        if self.initialized:
            return
        result = await self.rpc('initialize', {'protocolVersion': self.protocol, 'capabilities': {},
                                              'clientInfo': {'name': 'art2api', 'version': self.settings.version}})
        protocol = result.get('protocolVersion')
        if protocol not in {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}:
            raise GatewayError('Artlist selected an unsupported MCP protocol version', 'mcp_protocol_error')
        self.protocol = protocol
        await self.rpc('notifications/initialized', notification=True)
        self.initialized = True

    async def list_tools(self):
        async with self.lock:
            await self.initialize()
            cursor, tools = None, []
            for _ in range(50):
                result = await self.rpc('tools/list', {'cursor': cursor} if cursor else {})
                tools.extend(result.get('tools', []))
                cursor = result.get('nextCursor')
                if not cursor:
                    return tools
            raise GatewayError('MCP tool pagination limit reached', 'mcp_protocol_error')

    async def call(self, name, arguments, *, mutating=False):
        async with self.lock:
            await self.initialize()
            result = await self.rpc('tools/call', {'name': name, 'arguments': arguments}, mutating=mutating)
            return unwrap(result)

