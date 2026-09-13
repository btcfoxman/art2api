from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.errors import GatewayError
from app.network import client, upstream_url


class OAuth:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.locks = {}

    def lock(self, account_id):
        return self.locks.setdefault(account_id, asyncio.Lock())

    async def request(self, account, method, url, **kwargs):
        upstream_url(url, oauth=True)
        try:
            async with client(account['credentials']['proxy_url'], self.settings.request_timeout) as http:
                response = await http.request(method, url, **kwargs)
        except httpx.TransportError:
            raise GatewayError('Artlist authorization proxy connection failed', 'proxy_error', 502, retryable=True) from None
        if response.status_code >= 400:
            try:
                error = response.json().get('error', '')
            except ValueError:
                error = ''
            code = 'reauthorization_required' if error in {'invalid_grant', 'invalid_client'} else 'oauth_error'
            raise GatewayError(f'Artlist authorization returned HTTP {response.status_code}; reconnect the account if authorization expired', code, 502)
        try:
            data = response.json()
        except ValueError:
            raise GatewayError('Artlist authorization returned invalid JSON', 'oauth_protocol_error') from None
        if not isinstance(data, dict):
            raise GatewayError('Artlist authorization returned an invalid object', 'oauth_protocol_error')
        return data

    async def discover(self, account):
        resource = await self.request(account, 'GET', 'https://mcp.artlist.io/.well-known/oauth-protected-resource')
        metadata = await self.request(account, 'GET', 'https://mcp.artlist.io/.well-known/oauth-authorization-server')
        if 'https://auth.artlist.io/' not in resource.get('authorization_servers', []):
            raise GatewayError('Unexpected Artlist authorization server', 'oauth_protocol_error')
        if 'S256' not in metadata.get('code_challenge_methods_supported', []):
            raise GatewayError('Artlist authorization requires PKCE S256 support', 'oauth_protocol_error')
        for key in ('authorization_endpoint', 'token_endpoint'):
            upstream_url(metadata.get(key, ''), oauth=True)
        metadata['resource'] = upstream_url(resource.get('resource', ''))
        return metadata

    async def begin(self, account_id):
        async with self.lock(account_id):
            account = self.db.account(account_id, True)
            if account['active_tasks']:
                raise ValueError('存在未结束任务，暂不能重新绑定授权账号')
            metadata = await self.discover(account)
            redirect_uri = self.settings.public_base_url + '/oauth/callback'
            client_id = self.settings.oauth_client_id or account['credentials'].get('client_id')
            if not client_id and metadata.get('client_id_metadata_document_supported'):
                client_id = self.settings.public_base_url + '/oauth/client-metadata.json'
            if not client_id:
                registration_url = upstream_url(metadata.get('registration_endpoint', ''), oauth=True)
                registration = await self.request(account, 'POST', registration_url, json={
                    'client_name': 'ART2API', 'redirect_uris': [redirect_uri],
                    'grant_types': ['authorization_code', 'refresh_token'],
                    'response_types': ['code'], 'token_endpoint_auth_method': 'none',
                })
                client_id = registration.get('client_id')
                if not client_id:
                    raise GatewayError('Artlist did not return an OAuth client ID', 'oauth_protocol_error')
            verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            self.db.put_oauth_state(state, account_id, {
                'verifier': verifier, 'client_id': client_id, 'redirect_uri': redirect_uri,
                'metadata': metadata, 'proxy_version': account['proxy_version'],
            })
            self.db.update_credentials(account_id, {'client_id': client_id, 'oauth_metadata': metadata})
            query = urlencode({'response_type': 'code', 'client_id': client_id, 'redirect_uri': redirect_uri,
                               'scope': 'openid offline_access', 'state': state, 'code_challenge': challenge,
                               'code_challenge_method': 'S256', 'resource': metadata['resource'],
                               'audience': metadata['resource']})
            return metadata['authorization_endpoint'] + '?' + query

    async def callback(self, state, code):
        account_id, saved = self.db.take_oauth_state(state)
        async with self.lock(account_id):
            account = self.db.account(account_id, True)
            if account['proxy_version'] != saved['proxy_version'] or account['active_tasks']:
                raise ValueError('账号代理或任务状态已变化，请重新授权')
            data = await self.request(account, 'POST', saved['metadata']['token_endpoint'], data={
                'grant_type': 'authorization_code', 'code': code, 'code_verifier': saved['verifier'],
                'client_id': saved['client_id'], 'redirect_uri': saved['redirect_uri'],
                'resource': saved['metadata']['resource'],
            })
            self.save_tokens(account_id, data)
            self.db.update_account(account_id, enabled=False, status='unchecked', last_error='')
            self.db.event('oauth_connected', 'Artlist account authorization completed', account_id)
        return account_id

    def save_tokens(self, account_id, data):
        if not data.get('access_token') or str(data.get('token_type', 'Bearer')).lower() != 'bearer':
            raise GatewayError('Artlist did not issue a Bearer access token', 'oauth_protocol_error')
        fields = {'access_token': data['access_token'], 'expires_at': time.time() + float(data.get('expires_in', 3600))}
        if data.get('refresh_token'):
            fields['refresh_token'] = data['refresh_token']
        self.db.update_credentials(account_id, fields)

    async def access_token(self, account_id, force=False):
        async with self.lock(account_id):
            account = self.db.account(account_id, True)
            secret = account['credentials']
            if not force and secret.get('access_token') and secret.get('expires_at', 0) > time.time() + 60:
                return secret['access_token']
            if not secret.get('refresh_token') or not secret.get('client_id'):
                self.db.update_account(account_id, status='unauthorized', enabled=False)
                raise GatewayError('Artlist account needs authorization', 'reauthorization_required', 401)
            metadata = secret.get('oauth_metadata') or await self.discover(account)
            try:
                data = await self.request(account, 'POST', metadata['token_endpoint'], data={
                    'grant_type': 'refresh_token', 'client_id': secret['client_id'],
                    'refresh_token': secret['refresh_token'], 'resource': metadata['resource'],
                })
            except GatewayError as exc:
                if exc.code == 'reauthorization_required':
                    self.db.update_account(account_id, status='unauthorized', enabled=False)
                raise
            self.save_tokens(account_id, data)
            return data['access_token']
