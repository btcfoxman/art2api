from __future__ import annotations

import re


def parse_cookie_header(header):
    """Cookie request headers are not Set-Cookie records.

    Artlist's CookieScriptConsent contains unquoted JSON. SimpleCookie stops
    parsing at that value and can silently drop the later authentication cookie.
    """
    result = {}
    for part in header.split(';'):
        name, separator, value = part.strip().partition('=')
        if separator and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) and '\r' not in value and '\n' not in value:
            result[name] = value
    return result


def browser_cookies(header):
    result = []
    for name,value in parse_cookie_header(header).items():
        cookie = {'name':name,'value':value,'path':'/','secure':True}
        # Captured Artlist session/Cloudflare cookies use the parent domain.
        # __Host cookies must remain host-only; callback-url is also host-only.
        if name.startswith('__Host-') or name.endswith('.callback-url'):
            cookie['url'] = 'https://toolkit.artlist.io/'
        else:
            cookie['domain'] = '.artlist.io'
        if 'session-token' in name or name.startswith('__Host-') or name in {'__cf_bm','cf_clearance'}:
            cookie['httpOnly'] = True
        result.append(cookie)
    return result
