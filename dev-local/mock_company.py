"""Offline fixtures for the local stack, never a replacement for company SSO.

Only two synthetic identities are accepted. This module uses no external HTTP
client and has no real token or employee data. Do not deploy to company servers.
"""
from __future__ import annotations

import json
import re
import base64
import hashlib
import hmac
import html
import secrets
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

MODEL = 'deepseek-flash-v4.1'
MODEL_KEY = 'synthetic-local-model-fixture-only'
PERSONAL_KEY_SERVICE_SECRET = 'synthetic-local-personal-key-service'
PERSONAL_MODEL_KEYS = {
    'LOCAL-FIXTURE-A': 'synthetic-personal-model-key-A-only',
    'LOCAL-FIXTURE-B': 'synthetic-personal-model-key-B-only',
}
PERSONAL_MODELS = {
    'LOCAL-FIXTURE-A': [MODEL, 'glm-5.3-flash'],
    'LOCAL-FIXTURE-B': [MODEL],
}
USERS = {
    'a': {'userNo': 'LOCAL-FIXTURE-A', 'organizationCode': 'LOCAL-TEST', 'name': 'Local A'},
    'b': {'userNo': 'LOCAL-FIXTURE-B', 'organizationCode': 'LOCAL-TEST', 'name': 'Local B'},
}
IAM_CLIENT_ID = 'synthetic-client'
IAM_CLIENT_SECRET = 'synthetic-local-iam-client-secret'
IAM_DIRECTORY_API_KEY = 'synthetic-directory-key'
IAM_DIRECTORY_SECRET = 'synthetic-local-iam-directory-secret'
IAM_CALLBACK = 'http://127.0.0.1:28090/v1/iam/callback'
IAM_RETURN = 'http://127.0.0.1:28090/v1/iam/session'
IAM_AUTHORIZE = '/esc-sso/oauth2.0/authorize'
IAM_TOKEN = '/esc-sso/oauth2.0/accessToken'
IAM_PROFILE = '/esc-sso/oauth2.0/profile'


class IamFixtures:
    """Process-local, expiring synthetic grants; no real credentials accepted."""
    def __init__(self):
        self.lock = threading.Lock()
        self.codes = {}
        self.tokens = {}
        self.jwt_ids = {}

    def prune(self):
        now = time.time()
        for records in (self.codes, self.tokens):
            for key in list(records):
                if records[key]['expires'] <= now:
                    del records[key]
        for key in list(self.jwt_ids):
            if self.jwt_ids[key] <= now:
                del self.jwt_ids[key]


IAM = IamFixtures()


def directory_jwt_valid(header):
    try:
        scheme, token = header.split(' ', 1)
        head, payload, signature = token.split('.')
        decode = lambda text: base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
        info, claims = json.loads(decode(head)), json.loads(decode(payload))
        signed = hmac.new(IAM_DIRECTORY_SECRET.encode(), (head + '.' + payload).encode(), hashlib.sha256).digest()
        issued = claims.get('iat')
        jti = claims.get('jti')
        if (scheme != 'Bearer' or info.get('alg') != 'HS256'
                or not hmac.compare_digest(signed, decode(signature))
                or claims.get('iss') != IAM_DIRECTORY_API_KEY
                or type(issued) is not int or abs(time.time() - issued) > 300
                or uuid.UUID(jti).version != 4):
            return False
        with IAM.lock:
            IAM.prune()
            if jti in IAM.jwt_ids:
                return False
            IAM.jwt_ids[jti] = time.time() + 600
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'MegmeetLocalFixture/1'

    def log_message(self, *_args):
        # Do not emit request bodies, cookies or authorizations to logs.
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, cookies=()):
        self.send_response(302)
        self.send_header('Location', location)
        self.send_header('Cache-Control', 'no-store')
        for cookie in cookies:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def cookies(self):
        try:
            return {key: value.value for key, value in SimpleCookie(self.headers.get('Cookie', '')).items()}
        except Exception:
            return {}

    def iam_get(self, path, query):
        one = lambda key: query.get(key, [''])[0] if len(query.get(key, [])) == 1 else ''
        if path == '/esc-sso/heartbeat':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'OK')
            return
        if path == IAM_AUTHORIZE:
            if (one('client_id') != IAM_CLIENT_ID or one('response_type') != 'code'
                    or one('redirect_uri') != IAM_CALLBACK or not one('state')
                    or not one('oauth_timestamp')):
                self.reply(400, {'error': 'invalid_request'})
                return
            fixture = one('fixture') or self.cookies().get('local_iam_fixture')
            if fixture not in USERS:
                links = []
                for name in USERS:
                    selected = {key: value[0] for key, value in query.items() if len(value) == 1}
                    selected['fixture'] = name
                    links.append('<p><a href="' + html.escape(IAM_AUTHORIZE + '?' + urlencode(selected), quote=True)
                                 + '">Synthetic local account ' + name.upper() + '</a></p>')
                body = ('<!doctype html><meta charset="utf-8"><title>Local IAM fixture</title>'
                        '<h1>Local synthetic login</h1><p>No company account or password is used.</p>'
                        + ''.join(links)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            code, sid = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            with IAM.lock:
                IAM.prune()
                IAM.codes[code] = {'user': fixture, 'sid': sid, 'redirect': IAM_CALLBACK,
                                   'expires': time.time() + 120}
            self.redirect(IAM_CALLBACK + '?' + urlencode({'code': code, 'state': one('state')}), [
                'local_iam_fixture=' + fixture + '; Path=/esc-sso; HttpOnly; SameSite=Lax',
                'local_iam_session=' + sid + '; Path=/esc-sso; HttpOnly; SameSite=Lax',
            ])
            return
        if path == IAM_PROFILE:
            with IAM.lock:
                IAM.prune()
                token = IAM.tokens.get(one('access_token'))
            if token is None:
                self.reply(401, {'error': 'invalid_token'})
                return
            name = token['user']
            self.reply(200, {'id': 'synthetic-master-' + name, 'attributes': {
                'username': 'app-' + name, 'id': 'synthetic-master-' + name,
                'token_gtime': token['issued'], 'token_expired': '7200',
            }})
            return
        if path == '/esc-sso/logout':
            if one('service') != IAM_RETURN:
                self.reply(400, {'error': 'invalid_service'})
                return
            sid = self.cookies().get('local_iam_session')
            with IAM.lock:
                IAM.prune()
                for key in list(IAM.tokens):
                    if IAM.tokens[key]['sid'] == sid:
                        del IAM.tokens[key]
            self.redirect(IAM_RETURN, [
                'local_iam_fixture=; Path=/esc-sso; Max-Age=0; HttpOnly; SameSite=Lax',
                'local_iam_session=; Path=/esc-sso; Max-Age=0; HttpOnly; SameSite=Lax',
            ])
            return
        self.reply(404, {'detail': 'fixture route not found'})

    def iam_token(self, query):
        one = lambda key: query.get(key, [''])[0] if len(query.get(key, [])) == 1 else ''
        if (one('client_id') != IAM_CLIENT_ID
                or not hmac.compare_digest(one('client_secret').encode(), IAM_CLIENT_SECRET.encode())):
            self.reply(401, {'error': 'invalid_client'})
            return
        if (set(query) != {'grant_type', 'oauth_timestamp', 'client_id', 'client_secret', 'code', 'redirect_uri'}
                or one('grant_type') != 'authorization_code' or one('redirect_uri') != IAM_CALLBACK
                or len(one('oauth_timestamp')) != 13 or not one('oauth_timestamp').isdigit()):
            self.reply(400, {'error': 'invalid_request'})
            return
        with IAM.lock:
            IAM.prune()
            grant = IAM.codes.pop(one('code'), None)
            if grant:
                access = secrets.token_urlsafe(32)
                IAM.tokens[access] = {**grant, 'expires': time.time() + 7200,
                                      'issued': int(time.time() * 1000)}
        if grant is None:
            self.reply(400, {'error': 'invalid_grant'})
            return
        self.reply(200, {'access_token': access, 'token_type': 'bearer', 'expires_in': 7200,
                         'refresh_token': 'synthetic-unused-' + secrets.token_urlsafe(16)})

    def iam_directory(self, path):
        if not directory_jwt_valid(self.headers.get('Authorization', '')):
            self.reply(401, {'code': '401', 'msg': 'synthetic directory authorization required'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length < 4096:
                raise ValueError('invalid size')
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or set(payload) != {'page', 'size'}:
                raise ValueError('invalid page request')
            page, size = int(payload['page']), int(payload['size'])
            if not 1 <= size <= 1000 or page < 1:
                raise ValueError('invalid pagination')
        except (ValueError, TypeError, KeyError):
            self.reply(400, {'code': '400', 'msg': 'invalid synthetic page request'})
            return
        if path == '/esc-idm/api/v1/org/listAll':
            rows = [{'idt_org__org_code': 'LOCAL-TEST', 'idt_org__name': 'Local fixture department',
                     'idt_org__sup_org_code': '', 'idt_org__name_path': 'Local fixture department',
                     'idt_org__org_level': 1, 'idt_org__org_head': '', 'idt_org__status': 1}]
        else:
            rows = [{'app_account__account_no': 'app-' + key, 'idt_user__work_no': user['userNo'],
                     'idt_user__user_name': user['name'], 'app_account__status': 1,
                     'orgs': [{'idt_org__org_code': 'LOCAL-TEST'}],
                     'useTypes': [{'idt_user_type__code': 'employee', 'idt_user_type__name': 'Synthetic employee'}]}
                    for key, user in USERS.items()]
        self.reply(200, {'code': '0', 'msg': '操作成功', 'timestamp': int(time.time() * 1000),
                         'data': {'total': str(len(rows)), 'size': size, 'page': page,
                                  'list': rows[(page - 1) * size:page * size]}})

    def user(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            value = cookies.get('local_fixture_user')
            return USERS.get(value.value if value else '')
        except Exception:
            return None

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/esc-sso/'):
            self.iam_get(parsed.path, parse_qs(parsed.query))
        elif parsed.path == '/health':
            self.reply(200, {'status': 'healthy', 'mode': 'synthetic-local-only'})
        else:
            self.reply(404, {'detail': 'fixture route not found'})

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == '/api/external/user-credentials':
            # User-supplied company contract, using synthetic service/user tokens.
            # Identity comes only from the verified IAM token, never a body/header employee id.
            if (parsed.query or self.headers.get('X-API-Key') != PERSONAL_KEY_SERVICE_SECRET):
                self.reply(403, {'ok': False})
                return
            with IAM.lock:
                IAM.prune()
                grant = IAM.tokens.get(self.headers.get('X-IAM-Access-Token', ''))
            if grant is None:
                self.reply(401, {'ok': False})
                return
            try:
                if int(self.headers.get('Content-Length', '0')) != 0:
                    raise ValueError()
            except ValueError:
                self.reply(400, {'ok': False})
                return
            user = USERS[grant['user']]
            self.reply(200, {'ok': True, 'username': user['userNo'], 'iam_user_name': user['name'],
                            'api_key': PERSONAL_MODEL_KEYS[user['userNo']],
                            'models': [{'model_code': model, 'model_name': model}
                                       for model in PERSONAL_MODELS[user['userNo']]]})
            return
        if path == '/__synthetic/personal-key':
            if self.headers.get('Authorization') != 'Bearer ' + PERSONAL_KEY_SERVICE_SECRET:
                self.reply(403, {'detail': 'synthetic service authorization required'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length < 4096:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or set(data) != {'user_no'}:
                    raise ValueError()
                owner = data['user_no']
                key = PERSONAL_MODEL_KEYS.get(owner)
                if key is None:
                    self.reply(403, {'detail': 'synthetic employee not enabled'})
                    return
                self.reply(200, {'user_no': owner, 'api_key': key, 'expires_in': 120})
            except (ValueError, TypeError):
                self.reply(400, {'detail': 'invalid synthetic key request'})
            return
        if path == IAM_TOKEN:
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if (parsed.query or not 0 < length < 16384
                        or self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded'):
                    raise ValueError()
                form = parse_qs(self.rfile.read(length).decode('utf-8'), strict_parsing=True)
            except (ValueError, UnicodeError):
                self.reply(400, {'error': 'invalid_request'})
                return
            self.iam_token(form)
            return
        if path in {'/esc-idm/api/v1/org/listAll', '/esc-idm/api/v1/account/listAll'}:
            self.iam_directory(path)
            return
        if path == '/v1/auth/query-user':
            user = self.user()
            if user is None:
                self.reply(401, {'code': 401, 'data': None})
            else:
                self.reply(200, {'code': 0, 'data': user})
            return
        if path == '/v1/endpoint/verify-access':
            self.reply(403, {'detail': 'project capabilities not enabled in local fixture'})
            return
        if path not in {'/v1/chat/completions', '/personal-model/v1/chat/completions'}:
            self.reply(404, {'detail': 'fixture route not found'})
            return
        owner = None
        if path == '/personal-model/v1/chat/completions':
            owner = next((user for user, key in PERSONAL_MODEL_KEYS.items()
                          if self.headers.get('Authorization') == 'Bearer ' + key), None)
        authorized = owner is not None if path.startswith('/personal-model/') else self.headers.get('Authorization') == 'Bearer ' + MODEL_KEY
        if not authorized:
            self.reply(401, {'error': {'message': 'fixture credential required'}})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 262144:
                raise ValueError('invalid size')
            data = json.loads(self.rfile.read(length))
            permitted = PERSONAL_MODELS[owner] if owner else [MODEL]
            if not isinstance(data, dict) or data.get('model') not in permitted:
                raise ValueError('invalid model')
            messages = data.get('messages')
            if not isinstance(messages, list) or not messages:
                raise ValueError('invalid messages')
            content = None
            for message in reversed(messages):
                if message.get('role') != 'user':
                    continue
                candidate = message.get('content')
                if isinstance(candidate, list):
                    candidate = ''.join(b.get('text', '') for b in candidate if b.get('type') == 'text')
                # The native harness appends this synthetic runtime snapshot as
                # a user-role message. Echo the actual test input before it.
                if isinstance(candidate, str) and candidate.startswith('Current runtime context.'):
                    continue
                content = candidate
                break
            if not isinstance(content, str):
                raise ValueError('invalid message')
        except (ValueError, TypeError, AttributeError):
            self.reply(400, {'error': {'message': 'invalid fixture model request'}})
            return
        text = 'LOCAL_MODEL_OK: ' + content
        if owner is not None:
            text = 'PERSONAL_MODEL_OK[' + owner + ']: ' + content
        if content.startswith('Generate the session title from this JSON array'):
            text = '本地 DSH 桌面验证'
        # Explicit synthetic native-tool test, never arbitrary caller commands.
        # Only creates one UUID-named text file in the worker's own workspace.
        cloud_write = re.fullmatch(r'CLOUD_TOOL_WRITE_([a-f0-9]{32})', content)
        tool_call = None
        if owner and cloud_write:
            call_id = 'cloudfixture_' + cloud_write.group(1)
            already = any(m.get('role') == 'tool' and m.get('tool_call_id') == call_id for m in messages)
            tools = {t.get('function', {}).get('name') for t in data.get('tools', []) if isinstance(t, dict)}
            if not already and 'write' in tools:
                tool_call = {'id': call_id, 'type': 'function', 'function': {'name': 'write',
                    'arguments': json.dumps({'file_path': '/workspace/native-tool-' + cloud_write.group(1) + '.txt',
                                             'content': 'SYNTHETIC_NATIVE_TOOL_' + owner})}}
        if data.get('stream') is True:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            chunks = [({'role': 'assistant'}, None), ({'content': text}, None), ({}, 'stop')]
            if tool_call:
                chunks = [({'role': 'assistant'}, None), ({'tool_calls': [{'index': 0, **tool_call}]}, None), ({}, 'tool_calls')]
            for delta, finish in chunks:
                chunk = {'id': 'local-fixture-stream', 'object': 'chat.completion.chunk', 'model': data['model'],
                         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                self.wfile.write(('data: ' + json.dumps(chunk, ensure_ascii=False) + '\n\n').encode())
                self.wfile.flush()
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        if tool_call:
            self.reply(200, {'id': 'local-fixture-tool', 'object': 'chat.completion', 'model': data['model'],
                'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None, 'tool_calls': [tool_call]}}]})
            return
        self.reply(200, {
            'id': 'local-fixture-response', 'object': 'chat.completion', 'model': data['model'],
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': text}}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
        })


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8081), Handler).serve_forever()
