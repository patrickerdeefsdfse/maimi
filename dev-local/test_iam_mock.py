"""Synthetic IAM contract checks. No company network or credentials are used."""
import base64
import hashlib
import hmac
import json
import threading
import time
import unittest
import uuid
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import mock_company as fixture


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class IamMockTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), fixture.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_address[1])
        cls.client = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, path, method='GET', body=None, headers=None):
        req = Request(self.base + path, method=method, headers=headers or {},
                      data=None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode())
        try:
            response = self.client.open(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.headers, response.read()

    def authorize(self, user='a'):
        params = {'client_id': fixture.IAM_CLIENT_ID, 'response_type': 'code',
                  'redirect_uri': fixture.IAM_CALLBACK, 'state': uuid.uuid4().hex,
                  'oauth_timestamp': str(int(time.time() * 1000))}
        status, headers, _ = self.request(fixture.IAM_AUTHORIZE + '?' + urlencode(params),
                                         headers={'Cookie': 'local_iam_fixture=' + user})
        self.assertEqual(status, 302)
        target = urlsplit(headers['Location'])
        query = parse_qs(target.query)
        self.assertEqual(query['state'], [params['state']])
        self.assertEqual(target.scheme + '://' + target.netloc + target.path, fixture.IAM_CALLBACK)
        return query['code'][0], headers

    def token(self, code, secret=fixture.IAM_CLIENT_SECRET):
        params = {'client_id': fixture.IAM_CLIENT_ID, 'client_secret': secret,
                  'grant_type': 'authorization_code', 'redirect_uri': fixture.IAM_CALLBACK,
                  'oauth_timestamp': str(int(time.time() * 1000)), 'code': code}
        status, headers, body = self.request(fixture.IAM_TOKEN, method='POST', body=urlencode(params).encode(),
                                              headers={'Content-Type': 'application/x-www-form-urlencoded'})
        return status, json.loads(body)

    def jwt(self, **overrides):
        claims = {'iss': fixture.IAM_DIRECTORY_API_KEY, 'iat': int(time.time()), 'jti': str(uuid.uuid4())}
        claims.update(overrides)
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).rstrip(b'=')
        signed = encode({'alg': 'HS256', 'typ': 'JWT'}) + b'.' + encode(claims)
        signature = base64.urlsafe_b64encode(hmac.new(fixture.IAM_DIRECTORY_SECRET.encode(), signed, hashlib.sha256).digest()).rstrip(b'=')
        return 'Bearer ' + (signed + b'.' + signature).decode()

    def test_heartbeat_and_manual_selection(self):
        self.assertEqual(self.request('/esc-sso/heartbeat')[::2], (200, b'OK'))
        params = {'client_id': fixture.IAM_CLIENT_ID, 'response_type': 'code',
                  'redirect_uri': fixture.IAM_CALLBACK, 'state': 'fixture-state', 'oauth_timestamp': '1'}
        status, _, body = self.request(fixture.IAM_AUTHORIZE + '?' + urlencode(params))
        self.assertEqual(status, 200)
        self.assertIn(b'Synthetic local account A', body)
        self.assertIn(b'Synthetic local account B', body)
        params['redirect_uri'] = 'https://example.invalid/'
        self.assertEqual(self.request(fixture.IAM_AUTHORIZE + '?' + urlencode(params))[0], 400)

    def test_single_use_code_and_profile_identity(self):
        for user in ('a', 'b'):
            code, _ = self.authorize(user)
            self.assertEqual(self.token(code, 'wrong')[0], 401)
            status, token = self.token(code)
            self.assertEqual(status, 200)
            self.assertEqual(self.token(code)[0], 400)
            status, _, body = self.request(fixture.IAM_PROFILE + '?' + urlencode({'access_token': token['access_token']}))
            self.assertEqual(status, 200)
            profile = json.loads(body)
            self.assertEqual(profile['attributes']['username'], 'app-' + user)
            self.assertNotIn('workNo', profile['attributes'])

    def test_expired_code_denied(self):
        code, _ = self.authorize()
        with fixture.IAM.lock:
            fixture.IAM.codes[code]['expires'] = time.time() - 1
        self.assertEqual(self.token(code)[0], 400)

    def test_access_token_requires_form_without_url_secrets(self):
        code, _ = self.authorize()
        self.assertEqual(self.request(fixture.IAM_TOKEN + '?code=' + code, 'POST')[0], 400)
        self.assertEqual(self.request(fixture.IAM_TOKEN, 'POST', {'code': code})[0], 400)
        self.assertEqual(self.token(code)[0], 200)

    def test_external_personal_credentials_bound_to_current_iam_token(self):
        path = '/api/external/user-credentials'
        for user in ('a', 'b'):
            code, _ = self.authorize(user)
            token = self.token(code)[1]['access_token']
            headers = {'X-API-Key': fixture.PERSONAL_KEY_SERVICE_SECRET, 'X-IAM-Access-Token': token}
            self.assertEqual(self.request(path, 'POST')[0], 403)
            self.assertEqual(self.request(path, 'POST', headers={'X-API-Key': fixture.PERSONAL_KEY_SERVICE_SECRET})[0], 401)
            self.assertEqual(self.request(path, 'POST', {'username': 'forged'}, headers)[0], 400)
            status, _, raw = self.request(path, 'POST', headers={**headers, 'X-Employee-No': 'forged'})
            self.assertEqual(status, 200)
            result = json.loads(raw)
            owner = fixture.USERS[user]['userNo']
            self.assertEqual(result['username'], owner)
            self.assertEqual(result['api_key'], fixture.PERSONAL_MODEL_KEYS[owner])
            self.assertEqual([m['model_code'] for m in result['models']], fixture.PERSONAL_MODELS[owner])
            with fixture.IAM.lock:
                fixture.IAM.tokens[token]['expires'] = time.time() - 1
            self.assertEqual(self.request(path, 'POST', headers=headers)[0], 401)

    def test_logout_revokes_only_browser_grant(self):
        code_a, headers = self.authorize('a')
        code_b, _ = self.authorize('b')
        token_a, token_b = self.token(code_a)[1], self.token(code_b)[1]
        sid = next(c.split(';')[0] for c in headers.get_all('Set-Cookie') if c.startswith('local_iam_session='))
        status, _, _ = self.request('/esc-sso/logout?' + urlencode({'service': fixture.IAM_RETURN}), headers={'Cookie': sid})
        self.assertEqual(status, 302)
        for token, expected in ((token_a, 401), (token_b, 200)):
            self.assertEqual(self.request(fixture.IAM_PROFILE + '?' + urlencode({'access_token': token['access_token']}))[0], expected)

    def test_directory_jwt_replay_expiry_and_pagination(self):
        path = '/esc-idm/api/v1/account/listAll'
        body = {'page': '1', 'size': '1'}
        self.assertEqual(self.request(path, 'POST', body)[0], 401)
        for overrides in ({'iss': 'wrong'}, {'iat': int(time.time()) - 1000}, {'jti': 'wrong'}):
            self.assertEqual(self.request(path, 'POST', body, {'Authorization': self.jwt(**overrides)})[0], 401)
        auth = self.jwt()
        status, _, payload = self.request(path, 'POST', body, {'Authorization': auth})
        self.assertEqual(status, 200)
        first = json.loads(payload)
        self.assertEqual(first['code'], '0')
        self.assertEqual(first['msg'], '操作成功')
        self.assertEqual(first['data']['total'], '2')
        self.assertEqual(first['data']['list'][0]['app_account__account_no'], 'app-a')
        self.assertEqual(self.request(path, 'POST', body, {'Authorization': auth})[0], 401)
        status, _, payload = self.request(path, 'POST', {'page': '2', 'size': '1'}, {'Authorization': self.jwt()})
        self.assertEqual(json.loads(payload)['data']['list'][0]['idt_user__work_no'], 'LOCAL-FIXTURE-B')
        status, _, payload = self.request('/esc-idm/api/v1/org/listAll', 'POST', {'page': '1', 'size': '1000'}, {'Authorization': self.jwt()})
        self.assertEqual(json.loads(payload)['data']['list'][0]['idt_org__org_code'], 'LOCAL-TEST')


if __name__ == '__main__':
    unittest.main()
