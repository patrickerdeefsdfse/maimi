"""Local desktop native endpoint checks, only synthetic workspace and fixtures."""
import json
import hashlib
import re
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:28084'
client = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(path, headers=None, data=None):
    req = urllib.request.Request(BASE + path, headers=headers or {},
                                 data=None if data is None else json.dumps(data).encode())
    try:
        response = client.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read()


status, body = request('/__megmeet/health')
assert status == 200 and json.loads(body)['dsh'] == '0.1.6-alpha.2'
print('NATIVE_VERSION_READY: PASS')
status, body = request('/__megmeet/release')
release = json.loads(body)
assert status == 200 and release['frontend'] == '0.1.6-alpha.2'
assert set(release['packages'].values()) == {'0.1.6-alpha.2'}
assert release['branding'] == 'megmeet-assets-r5'
print('ALL_DSH_PACKAGES_SAME_RELEASE:', len(release['packages']))
status, body = request('/')
assert status == 200 and b'__DSH_BOOT__' in body
assert b'?token=' not in body
print('NATIVE_INDEX_NO_TOKEN_URL: PASS')
html = body.decode()
boot = json.loads(re.search(r'globalThis\["__DSH_BOOT__"\] = (\{.*?\})</script>', html).group(1))
entries = {row['id']: row['url'] for row in boot['entries']}
modules = ['@deepseek-ai/' + name for name in (
    'dsh-client-ui-conversation', 'dsh-client-ui-chat', 'dsh-client-ui-sidebar',
    'dsh-client-ui-settings-models', 'dsh-client-ui-brand-official')]
for name in modules:
    status, resource = request(entries[name])
    assert status == 200, name
    # Only upstream's generated source-map trailer is excluded from comparison.
    payload = re.sub(rb'//# sourceMappingURL=[^\r\n]*\r?\n$', b'', resource)
    assert hashlib.sha256(payload).hexdigest() == release['resources']['/plugins/' + name + '/client.js'], name
    if name.endswith('dsh-client-ui-brand-official'):
        assert b'data-megmeet-hero-image' in resource and b'data-megmeet-sidebar-image' in resource
        assert b'data-megmeet-brand-wordmark' in resource
    if name.endswith('dsh-client-ui-conversation'):
        assert 'children: "电亮AI新纪元"'.encode() in resource
        assert b'children: t("hero.preview")' not in resource
print('NATIVE_MEGMEET_BRAND_R5: PASS')
paths = [urlparse(urljoin(BASE + '/', p)).path for p in re.findall(r'(?:src|href)="([^"?]+\.(?:js|css))"', html)]
assert paths
for path in dict.fromkeys(paths):
    assert path in release['resources'], path
    status, resource = request(path)
    assert status == 200 and hashlib.sha256(resource).hexdigest() == release['resources'][path], path
print('SERVED_FRONTEND_AND_NATIVE_MODULE_HASHES: PASS')
for headers in ({'Origin': 'https://example.invalid'}, {'Host': 'example.invalid'}):
    assert request('/api/workspace/create', headers, {})[0] == 403
print('FOREIGN_HOST_AND_ORIGIN: DENIED')
data = {'type': 'client-request', 'rpcId': 'native-local-smoke', 'method': 'workspace/create',
        'payload': {'args': {'request': {'path': '/state/work'}}}}
status, body = request('/api/workspace/create', {'Content-Type': 'application/json', 'Origin': BASE}, data)
value = json.loads(body)['result']
assert status == 200 and value['ok'] and not value['value']['created']
assert value['value']['workspace']['path'] == '/state/work'
print('PERSISTENT_DEFAULT_WORKSPACE: PASS')
print('MODEL_SSO_SCOPE: SYNTHETIC_LOCAL_ONLY')
