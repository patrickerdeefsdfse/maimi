"""Real local Gateway/Redis + synthetic IAM/model acceptance; no company network."""
from __future__ import annotations

import argparse
from http.cookiejar import Cookie, CookieJar
import json
import uuid
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def run(base='http://127.0.0.1:28090'):
    if base.rstrip('/') != 'http://127.0.0.1:28090':
        raise ValueError('IAM smoke is restricted to the synthetic loopback 28090 stack')
    base = base.rstrip('/')

    def check(condition, label):
        if not condition:
            raise RuntimeError(label)
        print(label + ': PASS', flush=True)

    class Browser:
        def __init__(self):
            self.jar = CookieJar()
            self.client = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.jar), NoRedirect())

        def select_fixture(self, user):
            assert user in {'a', 'b'}
            self.jar.set_cookie(Cookie(0, 'local_iam_fixture', user, None, False, '127.0.0.1',
                                      False, False, '/esc-sso', True, False, None, True, None, None,
                                      {'HttpOnly': None}, False))

        def request(self, path, body=None, headers=None, method=None):
            target = urljoin(base + '/', path)
            parsed = urlsplit(target)
            if (parsed.scheme + '://' + parsed.netloc != base or parsed.username or parsed.password
                    or parsed.fragment):
                raise RuntimeError('IAM_SMOKE_NONLOCAL_REDIRECT_REJECTED')
            request_headers = dict(headers or {})
            if body is not None:
                request_headers.setdefault('Content-Type', 'application/json')
                request_headers.setdefault('Origin', base)
            request = Request(target, headers=request_headers, method=method,
                              data=None if body is None else json.dumps(body).encode())
            try:
                response = self.client.open(request, timeout=20)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read()
                payload = json.loads(raw) if 'application/json' in response.headers.get('Content-Type', '') else raw
                return response.status, response.headers, payload

        def login(self, user):
            self.select_fixture(user)
            status, headers, _ = self.request('/v1/iam/login')
            check(status in {302, 303} and urlsplit(headers.get('Location', '')).path == '/esc-sso/oauth2.0/authorize',
                  'IAM_LOGIN_START_' + user.upper())
            check(any(c.name == 'megmeet_iam_oauth_bind' and c.has_nonstandard_attr('HttpOnly') for c in self.jar),
                  'IAM_BROWSER_BINDING_HTTPONLY_' + user.upper())
            status, headers, _ = self.request(headers['Location'])
            check(status in {302, 303} and urlsplit(headers.get('Location', '')).path == '/v1/iam/callback',
                  'IAM_AUTHORIZATION_CALLBACK_' + user.upper())
            callback = headers['Location']
            status, headers, _ = self.request(callback)
            check(status in {302, 303} and headers.get('Location') == base + '/v1/iam/session',
                  'IAM_CODE_EXCHANGE_' + user.upper())
            check(self.request(callback)[0] == 400, 'IAM_CALLBACK_REPLAY_REJECTED_' + user.upper())
            status, _, data = self.request('/v1/iam/session')
            actor = data.get('user') or {}
            check(status == 200 and data.get('authenticated') is True
                  and actor.get('account_no') == 'app-' + user
                  and actor.get('user_no') == 'LOCAL-FIXTURE-' + user.upper()
                  and actor.get('organization_codes') == ['LOCAL-TEST']
                  and actor.get('auth_provider') == 'iam_direct',
                  'IAM_DIRECTORY_EMPLOYEE_MAPPING_' + user.upper())
            check('access_token' not in json.dumps(data) and 'refresh_token' not in json.dumps(data)
                  and any(c.name == 'megmeet_iam_session' and c.has_nonstandard_attr('HttpOnly') for c in self.jar),
                  'IAM_OPAQUE_SESSION_NO_UPSTREAM_TOKEN_' + user.upper())

    anonymous, a, b = Browser(), Browser(), Browser()
    status, _, data = anonymous.request('/ready')
    check(status == 200 and data == {'status': 'ready', 'database': 'ready', 'redis': 'ready'},
          'IAM_GATEWAY_DATABASE_REDIS_READY')
    check(anonymous.request('/esc-sso/heartbeat')[2] == b'OK', 'IAM_SYNTHETIC_HEARTBEAT')
    check(anonymous.request('/v1/iam/session')[0] == 401
          and anonymous.request('/v1/chat/history')[0] in {401, 403}, 'IAM_ANONYMOUS_REJECTED')
    check(anonymous.request('/v1/iam/session', headers={'Cookie': 'local_fixture_user=a; user_no=LOCAL-FIXTURE-A'})[0] == 401,
          'IAM_LEGACY_AND_FORGED_IDENTITY_REJECTED')
    for browser, user in ((a, 'a'), (b, 'b')):
        browser.login(user)
    conversation = 'local-iam-smoke-' + uuid.uuid4().hex
    histories = {}
    for browser, user in ((a, 'a'), (b, 'b')):
        marker = 'IAM_SYNTHETIC_' + user.upper() + '_' + conversation
        status, _, data = browser.request('/v1/chat/run', {
            'conversation_id': conversation, 'input': marker,
        })
        check(status == 200 and data.get('status') == 'completed'
              and data.get('text') == 'LOCAL_MODEL_OK: ' + marker
              and data.get('history_persisted') is True, 'IAM_AUTHENTICATED_MODEL_HISTORY_' + user.upper())
        status, _, data = browser.request('/v1/chat/history/' + conversation)
        check(status == 200 and len(data.get('messages', [])) == 2
              and all(marker in message['content']['text'] for message in data['messages']),
              'IAM_OWN_HISTORY_' + user.upper())
        histories[user] = data
    check(histories['a']['messages'][0]['message_id'] != histories['b']['messages'][0]['message_id'],
          'IAM_TWO_EMPLOYEE_HISTORY_SEPARATION')
    check(a.request('/v1/iam/logout', {}, {'Origin': 'https://example.invalid'})[0] == 403
          and a.request('/v1/iam/session')[0] == 200, 'IAM_FOREIGN_ORIGIN_LOGOUT_REJECTED')
    stale_session = next(c.value for c in a.jar if c.name == 'megmeet_iam_session')
    status, _, data = a.request('/v1/iam/logout', {})
    check(status == 200 and urlsplit(data.get('logout_url', '')).path == '/esc-sso/logout',
          'IAM_LOGOUT_LOCAL_SESSION_DELETED')
    check(a.request('/v1/iam/session')[0] == 401
          and anonymous.request('/v1/iam/session', headers={'Cookie': 'megmeet_iam_session=' + stale_session})[0] == 401,
          'IAM_REVOKED_REDIS_SESSION_REPLAY_REJECTED')
    status, headers, _ = a.request(data['logout_url'])
    check(status in {302, 303} and headers.get('Location') == base + '/v1/iam/session', 'IAM_PROVIDER_LOGOUT_RETURN')
    check(b.request('/v1/iam/session')[0] == 200, 'IAM_OTHER_ACCOUNT_UNAFFECTED_BY_LOGOUT')
    a.login('a')
    status, _, recovered = a.request('/v1/chat/history/' + conversation)
    check(status == 200 and recovered == histories['a'], 'IAM_HISTORY_AFTER_LOGOUT_LOGIN')
    print('LOCAL_IAM_GATEWAY_REDIS_HISTORY_SMOKE: PASS')
    print('COMPANY_IAM_PERSONAL_TOKENKEY_NATIVE_DSH_INTEGRATION: NOT_TESTED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:28090')
    run(parser.parse_args().url)
