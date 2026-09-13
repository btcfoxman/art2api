from __future__ import annotations

import asyncio
import ipaddress
import time

from app.browser import BrowserManager
from app.catalog import (PUBLIC_MODELS, build_arguments, local_schema, normalize_request, query_arguments,
                         task_identity, task_result, validate_profiles)
from app.errors import GatewayError
from app.mcp import MCPClient
from app.network import client, safe_error
from app.oauth import OAuth


class Service:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.oauth = OAuth(db, settings)
        self.browsers = BrowserManager(db, settings)
        self.clients = {}
        self.jobs = {}
        self.maintenance = None
        self.stopping = False

    def mcp(self, account_id):
        account = self.db.account(account_id)
        key = (account_id, account['proxy_version'])
        if key not in self.clients:
            self.clients[key] = MCPClient(account_id, self.db, self.oauth, self.settings)
        return self.clients[key]

    async def start(self):
        for task in self.db.tasks(active=True):
            if task['status'] == 'submitting' and not task['upstream_id']:
                self.db.update_task(task['id'], status='submission_unknown', error='进程在提交期间重启，上游是否接受未知；不会自动重提', error_code='submission_unknown')
            elif task['status'] != 'submission_unknown':
                self.schedule(task['id'])
        self.maintenance = asyncio.create_task(self.watchdog())

    async def stop(self):
        self.stopping = True
        if self.maintenance:
            self.maintenance.cancel()
        jobs = list(self.jobs.values())
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, *([self.maintenance] if self.maintenance else []), return_exceptions=True)
        await self.browsers.stop()

    def schedule(self, task_id):
        if task_id not in self.jobs:
            job = asyncio.create_task(self.run(task_id))
            self.jobs[task_id] = job
            job.add_done_callback(lambda _: self.jobs.pop(task_id, None))

    async def inspect_account(self, account_id):
        account = self.db.account(account_id, True)
        credentials = account['credentials']
        try:
            async with client(credentials['proxy_url'], self.settings.request_timeout) as http:
                response = await http.get('https://api.ipify.org?format=json')
                response.raise_for_status()
                address = str(ipaddress.ip_address(response.json()['ip']))
            self.db.update_account(account_id, egress_ip=address, checked_at=time.time())
            if self.db.account(account_id)['duplicate_egress']:
                self.db.update_account(account_id, enabled=False, status='proxy_conflict', last_error='出口 IP 与其他账号重复')
                raise ValueError('该出口 IP 已被其他账号使用')
            if not account['authorized']:
                await self.oauth.discover(account)
                self.db.update_account(account_id, status='unauthorized', last_error='代理正常，等待 Artlist 授权')
                return self.db.account(account_id)
            tools = await self.mcp(account_id).list_tools()
            self.db.update_account(account_id, tools=tools, status='ready', last_error='')
            self.db.event('account_checked', 'Proxy and Artlist MCP connection verified', account_id)
            return self.db.account(account_id)
        except Exception as exc:
            message = safe_error(exc, list(v for v in credentials.values() if isinstance(v, str)))
            self.db.update_account(account_id, status='error', enabled=False, last_error=message)
            self.db.event('account_check_failed', message, account_id)
            raise

    async def catalog(self, account_id, tool_name, arguments):
        account = self.db.account(account_id)
        tool = next((t for t in account['tools'] if t['name'] == tool_name), None)
        if not tool or not any(word in tool_name.lower() for word in ('list', 'get', 'search', 'available')) or not any(word in tool_name.lower() for word in ('model', 'capabilit', 'setting')):
            raise ValueError('仅允许调用已发现的模型或能力查询工具')
        from jsonschema import Draft202012Validator
        Draft202012Validator(local_schema(tool.get('inputSchema', {}))).validate(arguments)
        result = await self.mcp(account_id).call(tool_name, arguments)
        self.db.update_account(account_id, catalog=result if isinstance(result, dict) else {'models': result})
        return result

    def save_profiles(self, account_id, profiles):
        account = self.db.account(account_id)
        validate_profiles(profiles, account['tools'])
        self.db.update_account(account_id, profiles=profiles, enabled=False if not profiles else account['enabled'])
        return self.db.account(account_id)

    async def create(self, payload, idempotency_key=''):
        request = normalize_request(payload)
        candidates = []
        for account in self.db.accounts():
            profile = account['profiles'].get(request['model'])
            if not profile or not account['enabled']:
                continue
            try:
                build_arguments(request, profile, account['tools'])
            except ValueError:
                continue
            candidates.append((account['id'], profile))
        task, created = self.db.create_task(request, candidates, idempotency_key, self.settings.queue_limit)
        if created:
            self.db.event('task_queued', 'Task bound to account and proxy version', task['account_id'], task['id'])
            self.schedule(task['id'])
        return self.public_task(task)

    async def run(self, task_id):
        task = self.db.task(task_id)
        try:
            account = self.db.account(task['account_id'], True)
            if account['proxy_version'] != task['proxy_version']:
                raise GatewayError('Task proxy binding changed', 'proxy_binding_changed', ambiguous=bool(task['upstream_id']))
            mcp = self.mcp(account['id'])
            if not task['upstream_id']:
                if not account['enabled']:
                    raise GatewayError('Account disabled before submission', 'generation_rejected')
                self.db.update_task(task_id, status='preparing')
                # Complete all safe preflight operations before recording the mutation boundary.
                tools = await mcp.list_tools()
                await self.oauth.access_token(account['id'])
                arguments = build_arguments(task['request'], task['profile'], tools)
                self.db.update_task(task_id, status='submitting')
                data = await mcp.call(task['profile']['submit_tool'], arguments, mutating=True)
                upstream_id = task_identity(data, task['profile'])
                if not upstream_id:
                    raise GatewayError('Artlist submit response has no generation ID; task will not be resubmitted', 'submission_unknown', ambiguous=True)
                self.db.update_task(task_id, upstream_id=upstream_id, status='running')
                task = self.db.task(task_id)
                self.db.event('upstream_submitted', 'Artlist generation ID persisted', account['id'], task_id)
            failures = 0
            while not self.stopping:
                if time.time() - task['created_at'] > self.settings.task_timeout:
                    raise GatewayError('Artlist generation query deadline exceeded', 'upstream_outcome_unknown', ambiguous=True)
                try:
                    tools = self.db.account(account['id'])['tools']
                    data = await mcp.call(task['profile']['status_tool'], query_arguments(task['profile'], task['upstream_id'], tools))
                    status, url = task_result(data, task['profile'])
                    failures = 0
                    if status == 'succeeded':
                        self.db.update_task(task_id, status=status, result={'video_url': url}, error='', error_code='')
                        self.db.event('task_succeeded', 'Video result ready', account['id'], task_id)
                        return
                    if status == 'failed':
                        self.db.update_task(task_id, status='failed', error='Artlist explicitly reported generation failure', error_code='generation_failed')
                        return
                except GatewayError as exc:
                    if not exc.retryable and exc.code != 'reauthorization_required':
                        raise GatewayError(str(exc), 'upstream_outcome_unknown', ambiguous=True) from exc
                    failures += 1
                    if exc.code == 'reauthorization_required':
                        await self.oauth.access_token(account['id'], force=True)
                    self.db.update_task(task_id, error=safe_error(exc), error_code=exc.code)
                await asyncio.sleep(min(60, self.settings.poll_interval * max(1, failures)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            fresh = self.db.task(task_id)
            ambiguous = getattr(exc, 'ambiguous', False) or bool(fresh['upstream_id']) or fresh['status'] == 'submitting'
            # Explicit tool rejections are the only safe failure after entering submission.
            if isinstance(exc, GatewayError) and exc.code == 'generation_rejected':
                ambiguous = False
            status = 'submission_unknown' if ambiguous else 'failed'
            code = 'upstream_outcome_unknown' if ambiguous and fresh['upstream_id'] else 'submission_unknown' if ambiguous else getattr(exc, 'code', 'validation_error')
            credentials = self.db.account(task['account_id'], True)['credentials']
            message = safe_error(exc, [v for v in credentials.values() if isinstance(v, str)])
            self.db.update_task(task_id, status=status, error=message, error_code=code)
            self.db.event(status, message, task['account_id'], task_id)

    async def recover_unknown(self, task_id, upstream_id):
        task = self.db.task(task_id)
        if task['status'] != 'submission_unknown' or task_id in self.jobs:
            raise ValueError('只可恢复结果未知且已停止本地查询的任务')
        if not upstream_id:
            raise ValueError('必须提供该账号真实的 Artlist generation ID')
        self.db.update_task(task_id, upstream_id=upstream_id, status='running', error='', error_code='')
        self.schedule(task_id)
        return self.public_task(self.db.task(task_id))

    async def watchdog(self):
        while True:
            await asyncio.sleep(15)
            await self.browsers.cleanup()
            for task in self.db.tasks(active=True):
                if time.time() - task['created_at'] > self.settings.task_timeout and task['status'] != 'submission_unknown':
                    job = self.jobs.get(task['id'])
                    if job:
                        job.cancel()
                        await asyncio.gather(job, return_exceptions=True)
                    ambiguous = bool(task['upstream_id']) or task['status'] == 'submitting'
                    self.db.update_task(task['id'], status='submission_unknown' if ambiguous else 'failed',
                                        error='Task deadline exceeded', error_code='upstream_outcome_unknown' if ambiguous else 'task_timeout')

    def models(self):
        available = {}
        for account in self.db.accounts():
            if account['enabled'] and account['status'] == 'ready' and not account['duplicate_egress']:
                for model, profile in account['profiles'].items():
                    available.setdefault(model, {'id': model, 'object': 'model', 'owned_by': 'art2api', 'capabilities': []})['capabilities'].append(profile['constraints'])
        return {'object': 'list', 'data': list(available.values())}

    @staticmethod
    def public_task(task):
        result = task['result']
        url = result.get('video_url', '')
        status = 'failed' if task['status'] == 'submission_unknown' else task['status']
        return {'id': task['id'], 'object': 'video', 'status': status, 'model': task['request']['model'],
                'progress': 100 if status in {'succeeded', 'failed'} else 10 if status == 'queued' else 30,
                'created_at': task['created_at'], 'updated_at': task['updated_at'],
                'content': {'video_url': url} if url else {}, 'data': [{'url': url, 'type': 'video'}] if url else [],
                'error': {'code': task['error_code'], 'message': task['error']} if task['error_code'] and status == 'failed' else None}
