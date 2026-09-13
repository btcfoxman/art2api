from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit, unquote

import httpx

from app.errors import GatewayError


def normalize_proxy(value: str) -> str:
    value = str(value or "").strip()
    if re.fullmatch(r"xray:\d{1,5}", value):
        value = "socks5://" + value
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port:
            raise ValueError()
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError()
        if not 1 <= parsed.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("必须配置有效代理：使用 http(s)://host:port 或 socks5://host:port；不支持直连") from None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def mask_proxy(value: str) -> str:
    parsed = urlsplit(value)
    host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    return f"{parsed.scheme}://{'***:***@' if parsed.username is not None else ''}{host}:{parsed.port}"


def browser_proxy(value: str) -> dict:
    parsed = urlsplit(normalize_proxy(value))
    scheme = "socks5" if parsed.scheme == "socks5h" else parsed.scheme
    if scheme == "socks5" and parsed.username:
        raise ValueError("浏览器授权使用无认证 SOCKS5（例如 Xray）或带认证 HTTP 代理；Chromium 不支持 SOCKS5 用户名认证")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    result = {"server": f"{scheme}://{host}:{parsed.port}"}
    if parsed.username:
        result.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return result


def client(proxy: str, timeout: float = 60, **kwargs) -> httpx.AsyncClient:
    # There is deliberately no direct connection path, including OAuth and media.
    return httpx.AsyncClient(proxy=normalize_proxy(proxy), trust_env=False,
                             timeout=httpx.Timeout(timeout, connect=min(timeout, 20)),
                             follow_redirects=False, **kwargs)


def upstream_url(value: str, *, oauth=False) -> str:
    parsed = urlsplit(value)
    allowed = {"mcp.artlist.io", "auth.artlist.io"} if oauth else {"mcp.artlist.io"}
    if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise GatewayError("upstream endpoint is outside the Artlist allowlist", "invalid_endpoint", 422)
    return value


def public_media_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("素材必须使用公网 HTTPS URL")
    if parsed.hostname in {"localhost", "xray", "host.docker.internal"} or parsed.hostname.endswith((".local", ".internal")):
        raise ValueError("素材地址不能指向内部网络")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise ValueError("素材地址不能指向内部网络")
    except ValueError as exc:
        if "内部" in str(exc):
            raise
    return value


def safe_error(exc: Exception, secrets: list[str] = ()) -> str:
    message = str(exc)
    message = re.sub(r"(https?|socks5h?)://[^\s/@]+:[^\s/@]+@", r"\1://***:***@", message)
    message = re.sub(r"(?i)(access_token|refresh_token|client_secret|code_verifier|authorization)[\s\"':=]+[^\s,}\"]+", r"\1=<redacted>", message)
    for value in secrets:
        if value:
            message = message.replace(value, "<redacted>")
    return message[:600]

