from app.cookies import browser_cookies, parse_cookie_header


def test_consent_json_never_truncates_later_login_cookie():
    header='first=1; CookieScriptConsent={"action":"accept","categories":["necessary"]}; __Secure-session.artlist-prod.session-token=private.jwt; last=2'
    parsed=parse_cookie_header(header)
    assert len(parsed)==4 and parsed['__Secure-session.artlist-prod.session-token']=='private.jwt'
    assert parsed['CookieScriptConsent'].startswith('{"action"')


def test_artlist_session_domain_and_host_cookie_rules_match_capture():
    cookies={c['name']:c for c in browser_cookies('__Secure-session.artlist-prod.session-token=private; __Host-session.artlist-prod.csrf-token=csrf; __cf_bm=cf')}
    assert cookies['__Secure-session.artlist-prod.session-token']['domain']=='.artlist.io'
    assert cookies['__cf_bm']['domain']=='.artlist.io'
    host=cookies['__Host-session.artlist-prod.csrf-token']
    assert 'domain' not in host and host['url']=='https://toolkit.artlist.io/' and host['secure']
