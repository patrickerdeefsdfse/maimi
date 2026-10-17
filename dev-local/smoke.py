"""Exercise the real Gateway with only synthetic local SSO/model dependencies."""
from __future__ import annotations

import argparse
import json
import uuid
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


def run(base):
    parsed = urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', 'gateway'}
            or parsed.username or parsed.password or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment):
        raise ValueError('only the local Gateway is allowed')
    base = base.rstrip('/')
    client = build_opener(ProxyHandler({}))

    def request(path, user=None, body=None):
        headers = {'Content-Type': 'application/json'}
        if user:
            headers['Cookie'] = 'local_fixture_user=' + user
        req = Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            response = client.open(req, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def check(value, label):
        if not value:
            raise RuntimeError(label)
        print(label + ': PASS')

    status, data = request('/health')
    check(status == 200 and data.get('status') == 'healthy', 'LOCAL_GATEWAY_HEALTH')
    status, data = request('/ready')
    check(status == 200 and data == {'status': 'ready', 'database': 'ready', 'redis': 'ready'},
          'LOCAL_DATABASE_REDIS_MIGRATIONS')
    check(request('/v1/chat/history')[0] == 403, 'LOCAL_ANONYMOUS_REJECTED')
    check(request('/v1/system/sso-check', 'unknown')[0] == 403, 'LOCAL_UNKNOWN_IDENTITY_REJECTED')
    for user in ('a', 'b'):
        status, data = request('/v1/system/sso-check', user)
        check(status == 200 and data.get('authenticated') is True, 'LOCAL_SSO_' + user.upper())
    conversation = 'local-smoke-' + uuid.uuid4().hex
    for user in ('a', 'b'):
        marker = 'SYNTHETIC_' + user.upper() + '_' + conversation
        status, data = request('/v1/chat/run', user, {'conversation_id': conversation, 'input': marker})
        check(status == 200 and data.get('status') == 'completed'
              and data.get('text') == 'LOCAL_MODEL_OK: ' + marker
              and data.get('history_persisted') is True, 'LOCAL_MODEL_HISTORY_' + user.upper())
        status, data = request('/v1/chat/history/' + conversation, user)
        check(status == 200 and len(data.get('messages', [])) == 2
              and all(marker in message['content']['text'] for message in data['messages']),
              'LOCAL_OWN_HISTORY_' + user.upper())
    # Same client conversation ID must have distinct per-user stored records.
    _, own_a = request('/v1/chat/history/' + conversation, 'a')
    _, own_b = request('/v1/chat/history/' + conversation, 'b')
    check(own_a['messages'][0]['message_id'] != own_b['messages'][0]['message_id']
          and all('SYNTHETIC_B_' not in m['content']['text'] for m in own_a['messages'])
          and all('SYNTHETIC_A_' not in m['content']['text'] for m in own_b['messages']),
          'LOCAL_GATEWAY_HISTORY_SEPARATION')
    print('LOCAL_STACK_SMOKE: PASS')
    print('NATIVE_DSH_COMPANY_SSO_REAL_MODEL: NOT_TESTED')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:28090')
    run(parser.parse_args().url)
