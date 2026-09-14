from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from app.errors import GatewayError
from app.network import mask_proxy, normalize_proxy


ACTIVE = ("queued", "preparing", "submitting", "running", "submission_unknown")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Database:
    def __init__(self, path: Path, encryption_key: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(encryption_key.encode())
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                secret TEXT NOT NULL, proxy_fingerprint TEXT NOT NULL UNIQUE,
                proxy_version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'unauthorized',
                max_concurrency INTEGER NOT NULL DEFAULT 1, egress_ip TEXT NOT NULL DEFAULT '',
                checked_at REAL, last_error TEXT NOT NULL DEFAULT '', tools TEXT NOT NULL DEFAULT '[]',
                catalog TEXT NOT NULL DEFAULT '{}', profiles TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                proxy_version INTEGER NOT NULL, status TEXT NOT NULL,
                request TEXT NOT NULL, profile TEXT NOT NULL, upstream_id TEXT NOT NULL DEFAULT '',
                result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', error_code TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
            CREATE TABLE IF NOT EXISTS oauth_states (
                state_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                secret TEXT NOT NULL, expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, task_id TEXT,
                kind TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_verifications (
                digest TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
                secret TEXT NOT NULL, expires_at REAL NOT NULL, task_id TEXT UNIQUE
            );
            CREATE TABLE IF NOT EXISTS runtime_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), value TEXT NOT NULL
            );
        """)
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(tasks)')}
        if 'query_deadline' not in columns:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN query_deadline REAL NOT NULL DEFAULT 0')
        if 'cleared_at' not in columns:
            self.conn.execute('ALTER TABLE tasks ADD COLUMN cleared_at REAL NOT NULL DEFAULT 0')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def seal(self, value):
        return self.fernet.encrypt(dumps(value).encode()).decode()

    def unseal(self, value):
        return json.loads(self.fernet.decrypt(value.encode()))

    def account(self, account_id, private=False):
        with self.lock:
            row = self.conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise KeyError("account not found")
            result = dict(row)
            secret = self.unseal(result.pop("secret"))
            result.pop("proxy_fingerprint")
            result["proxy_url_masked"] = mask_proxy(secret["proxy_url"])
            result['backend'] = secret.get('backend', 'mcp')
            result["authorized"] = bool(secret.get('web_cookie') if result['backend'] == 'web' else secret.get("access_token"))
            result['verification_ready'] = self.has_verification(account_id) if result['backend'] == 'web' else True
            result["token_expires_at"] = secret.get("expires_at")
            result["enabled"] = bool(result["enabled"])
            for key in ("tools", "catalog", "profiles"):
                result[key] = json.loads(result[key])
            result["active_tasks"] = self.active_count(account_id)
            result["duplicate_egress"] = bool(result["egress_ip"] and self.conn.execute(
                "SELECT 1 FROM accounts WHERE id!=? AND egress_ip=? LIMIT 1", (account_id, result["egress_ip"])).fetchone())
            if private:
                result["credentials"] = secret
            return result

    def accounts(self, private=False):
        with self.lock:
            ids = self.conn.execute("SELECT id FROM accounts ORDER BY created_at").fetchall()
            return [self.account(row["id"], private) for row in ids]

    def active_count(self, account_id):
        marks = ",".join("?" for _ in ACTIVE)
        return self.conn.execute(f"SELECT COUNT(*) FROM tasks WHERE account_id=? AND status IN ({marks})", (account_id, *ACTIVE)).fetchone()[0]

    def save_account(self, values, account_id=None):
        with self.transaction() as con:
            if account_id:
                old = self.account(account_id, True)
                secret = old["credentials"]
                if 'backend' in values and values['backend'] != secret.get('backend', 'mcp'):
                    if self.active_count(account_id):
                        raise ValueError('存在未结束任务，不能切换账号接入方式')
                    if values['backend'] not in {'web', 'mcp'}:
                        raise ValueError('invalid backend')
                    secret['backend'] = values['backend']
                    con.execute('DELETE FROM web_verifications WHERE account_id=? AND task_id IS NULL', (account_id,))
                    con.execute("UPDATE accounts SET secret=?,enabled=0,status='unchecked',tools='[]',profiles='{}',catalog='{}' WHERE id=?", (self.seal(secret), account_id))
                if values.get("proxy_url"):
                    proxy = normalize_proxy(values["proxy_url"])
                    if proxy != secret["proxy_url"]:
                        if self.active_count(account_id):
                            raise ValueError("存在未结束任务，不能更换账号代理")
                        secret["proxy_url"] = proxy
                        fingerprint = hashlib.sha256(proxy.encode()).hexdigest()
                        con.execute("UPDATE accounts SET secret=?, proxy_fingerprint=?, proxy_version=proxy_version+1, enabled=0, status='unchecked', egress_ip='', checked_at=NULL WHERE id=?", (self.seal(secret), fingerprint, account_id))
                        con.execute('DELETE FROM web_verifications WHERE account_id=? AND task_id IS NULL', (account_id,))
                if values.get("enabled"):
                    fresh = self.account(account_id, True)
                    if fresh["status"] != "ready" or not fresh["authorized"] or not fresh["profiles"] or fresh["duplicate_egress"]:
                        raise ValueError("启用前必须完成代理与授权检测、模型配置，且出口不能与其他账号重复")
                allowed = {"name", "max_concurrency", "enabled"}
                for key in allowed & values.keys():
                    con.execute(f"UPDATE accounts SET {key}=? WHERE id=?", (values[key], account_id))
                con.execute("UPDATE accounts SET updated_at=? WHERE id=?", (time.time(), account_id))
            else:
                proxy = normalize_proxy(values.get("proxy_url", ""))
                account_id = uuid.uuid4().hex
                con.execute("INSERT INTO accounts (id,name,secret,proxy_fingerprint,max_concurrency,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (account_id, values["name"], self.seal({"proxy_url": proxy, 'backend': values.get('backend', 'mcp')}), hashlib.sha256(proxy.encode()).hexdigest(), values.get("max_concurrency", 1), time.time(), time.time()))
        return self.account(account_id)

    def update_credentials(self, account_id, fields):
        with self.transaction() as con:
            row = con.execute("SELECT secret FROM accounts WHERE id=?", (account_id,)).fetchone()
            secret = self.unseal(row["secret"])
            secret.update(fields)
            con.execute("UPDATE accounts SET secret=?,updated_at=? WHERE id=?", (self.seal(secret), time.time(), account_id))

    def update_web_session_if_current(self, account_id, original, browser):
        """A warm browser must not overwrite a newer HTTP/imported login."""
        with self.transaction() as con:
            current = self.account(account_id, True)['credentials']
            if any(current.get(key) != original.get(key) for key in ('web_cookie', 'web_user_id', 'proxy_url')):
                return False
            current.update(web_cookie=browser['cookie'], web_user_agent=browser['user_agent'])
            con.execute("UPDATE accounts SET secret=?,updated_at=? WHERE id=?", (self.seal(current), time.time(), account_id))
            return True

    def update_account(self, account_id, **fields):
        allowed = {"status", "enabled", "egress_ip", "checked_at", "last_error", "tools", "catalog", "profiles"}
        if not fields.keys() <= allowed:
            raise ValueError("invalid account fields")
        with self.transaction() as con:
            for key, value in fields.items():
                if key in {"tools", "catalog", "profiles"}:
                    value = dumps(value)
                con.execute(f"UPDATE accounts SET {key}=?,updated_at=? WHERE id=?", (value, time.time(), account_id))

    def delete_account(self, account_id):
        with self.transaction() as con:
            account = self.account(account_id)
            if account["enabled"] or self.active_count(account_id):
                raise ValueError("请先停用账号，并等待所有任务结束")
            if con.execute("SELECT 1 FROM tasks WHERE account_id=? LIMIT 1", (account_id,)).fetchone():
                raise ValueError("账号有关联历史任务，请保留停用状态以便审计")
            con.execute("DELETE FROM oauth_states WHERE account_id=?", (account_id,))
            con.execute('DELETE FROM web_verifications WHERE account_id=?', (account_id,))
            con.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def put_oauth_state(self, state, account_id, value):
        with self.transaction() as con:
            con.execute("DELETE FROM oauth_states WHERE account_id=? OR expires_at<?", (account_id, time.time()))
            con.execute("INSERT INTO oauth_states VALUES (?,?,?,?)", (hashlib.sha256(state.encode()).hexdigest(), account_id, self.seal(value), time.time()+600))

    def take_oauth_state(self, state):
        with self.transaction() as con:
            digest = hashlib.sha256(state.encode()).hexdigest()
            row = con.execute("SELECT * FROM oauth_states WHERE state_hash=? AND expires_at>?", (digest, time.time())).fetchone()
            if not row:
                raise ValueError("授权状态无效、已使用或已过期")
            con.execute("DELETE FROM oauth_states WHERE state_hash=?", (digest,))
            return row["account_id"], self.unseal(row["secret"])

    def task(self, task_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise KeyError("task not found")
            result = dict(row)
            for key in ("request", "profile", "result"):
                result[key] = json.loads(result[key])
            return result

    def tasks(self, active=False, limit=100, offset=0):
        with self.lock:
            if active:
                marks = ','.join('?' for _ in ACTIVE)
                rows = self.conn.execute(f"SELECT id FROM tasks WHERE status IN ({marks}) ORDER BY created_at", ACTIVE).fetchall()
            else:
                rows = self.conn.execute("SELECT id FROM tasks WHERE cleared_at=0 OR status NOT IN ('succeeded','failed') ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [self.task(row['id']) for row in rows]

    def task_summary(self):
        with self.lock:
            counts = {row['status']: row['n'] for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM tasks WHERE cleared_at=0 OR status NOT IN ('succeeded','failed') GROUP BY status")}
            return {'total': sum(counts.values()), 'active': sum(counts.get(s, 0) for s in ACTIVE),
                    'succeeded': counts.get('succeeded', 0), 'failed': counts.get('failed', 0),
                    'unknown': counts.get('submission_unknown', 0)}

    def clear_completed_tasks(self):
        # Retain task IDs, results and idempotency keys for channel queries/retries.
        # A positive status allowlist also protects unknown and future task states.
        with self.transaction() as con:
            count = con.execute("UPDATE tasks SET cleared_at=? WHERE cleared_at=0 AND status IN ('succeeded','failed')", (time.time(),)).rowcount
            if count:
                self.event('tasks_cleared', f'已从最近任务列表清空 {count} 条已完成或失败的任务')
            return count

    def runtime_settings(self):
        with self.lock:
            row = self.conn.execute('SELECT value FROM runtime_settings WHERE id=1').fetchone()
            return json.loads(row['value']) if row else {}

    def save_runtime_settings(self, values):
        with self.transaction() as con:
            current = self.runtime_settings()
            current.update(values)
            con.execute('INSERT INTO runtime_settings (id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (dumps(current),))

    def pin_task_deadlines(self, timeout):
        with self.transaction() as con:
            con.execute('UPDATE tasks SET query_deadline=created_at+? WHERE query_deadline=0', (timeout,))

    def create_task(self, request, candidates, idempotency_key, queue_limit, task_timeout=3600):
        digest = hashlib.sha256(dumps(request).encode()).hexdigest()
        with self.transaction() as con:
            if idempotency_key:
                old = con.execute("SELECT id,request_hash FROM tasks WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if old:
                    if old['request_hash'] != digest:
                        raise GatewayError("idempotency key was already used with another request", "idempotency_conflict", 409)
                    return self.task(old['id']), False
            if sum(self.active_count(a['id']) for a in self.accounts()) >= queue_limit:
                raise GatewayError("ARTAPI generation queue is full", "entitlement_unavailable", 503)
            eligible = []
            for account_id, profile in candidates:
                account = self.account(account_id)
                if account['enabled'] and account['status'] == 'ready' and not account['duplicate_egress'] and self.active_count(account_id) < account['max_concurrency']:
                    eligible.append((account, profile))
            if not eligible:
                raise GatewayError("ARTAPI has no available authorized proxy account or capacity for this model", "entitlement_unavailable", 503)
            account, profile = min(eligible, key=lambda item: (item[0]['active_tasks'], item[0]['updated_at']))
            task_id = 'art_' + uuid.uuid4().hex
            con.execute("INSERT INTO tasks (id,account_id,proxy_version,status,request,profile,idempotency_key,request_hash,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (task_id, account['id'], account['proxy_version'], 'queued', dumps(request), dumps(profile), idempotency_key or None, digest, time.time(), time.time()))
            con.execute('UPDATE tasks SET query_deadline=created_at+? WHERE id=?', (task_timeout, task_id))
            if profile.get('backend') == 'web':
                verification = con.execute('SELECT digest FROM web_verifications WHERE account_id=? AND task_id IS NULL AND expires_at>? ORDER BY expires_at LIMIT 1', (account['id'], time.time()+30)).fetchone()
                if verification:
                    con.execute('UPDATE web_verifications SET task_id=? WHERE digest=?', (task_id, verification['digest']))
            return self.task(task_id), True

    def has_verification(self, account_id):
        return bool(self.conn.execute('SELECT 1 FROM web_verifications WHERE account_id=? AND task_id IS NULL AND expires_at>? LIMIT 1', (account_id, time.time()+30)).fetchone())

    def save_verification(self, account_id, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 16000 or any(c.isspace() for c in token):
            raise ValueError('网页验证令牌格式无效')
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.transaction() as con:
            if self.account(account_id)['backend'] != 'web':
                raise ValueError('仅网页账号支持网页验证')
            if con.execute('SELECT 1 FROM web_verifications WHERE digest=?', (digest,)).fetchone():
                raise ValueError('验证令牌已登记或已使用，不能重放')
            con.execute('INSERT INTO web_verifications VALUES (?,?,?,?,NULL)', (digest, account_id, self.seal({'token': token}), time.time()+240))

    def take_verification(self, task_id):
        with self.transaction() as con:
            row = con.execute('SELECT * FROM web_verifications WHERE task_id=?', (task_id,)).fetchone()
            if not row:
                return ''
            if row['expires_at'] <= time.time() or not row['secret']:
                raise GatewayError('网页验证已过期，需要重新完成正常网页验证', 'generation_rejected', 422)
            token = self.unseal(row['secret'])['token']
            con.execute("UPDATE web_verifications SET secret='' WHERE task_id=?", (task_id,))
            return token

    def update_task(self, task_id, **fields):
        if not fields.keys() <= {'status','upstream_id','result','error','error_code','query_deadline'}:
            raise ValueError('invalid task fields')
        with self.transaction() as con:
            for key,value in fields.items():
                if key == 'result': value = dumps(value)
                con.execute(f'UPDATE tasks SET {key}=?,updated_at=? WHERE id=?', (value,time.time(),task_id))

    def event(self, kind, detail, account_id=None, task_id=None):
        with self.lock:
            self.conn.execute('INSERT INTO events (kind,detail,account_id,task_id,created_at) VALUES (?,?,?,?,?)', (kind,detail,account_id,task_id,time.time()))

    def events(self, limit=100):
        with self.lock:
            return [dict(r) for r in self.conn.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]

    def close(self):
        self.conn.close()
