"""Actual local HTTP integration; synthetic identities and explicitly seeded scope only."""
import base64
import hashlib
from uuid import uuid4

from desktop_auth_smoke import Browser, PREFIX, check, login


def run():
    a, ga = login("a")
    b, gb = login("b")
    ta, tb = ga["access_token"], gb["access_token"]
    try:
        status, _, workspaces = a.request('/v1/cloud/workspaces', token=ta)
        check(status == 200 and len(workspaces['items']) == 1, 'CLOUD_EXPLICIT_GRANT_A')
        wid = workspaces['items'][0]['workspace_id']
        check(b.request('/v1/cloud/workspaces', token=tb)[2]['items'] == [], 'CLOUD_NO_AUTOGRANT_B')
        status, _, plugins = a.request(f'/v1/cloud/workspaces/{wid}/plugins', token=ta)
        check(status == 200 and plugins['items'][0]['simulation'], 'CLOUD_EXPLICIT_PLUGIN_CATALOG')
        p = plugins['items'][0]
        rid = str(uuid4())
        raw = b'LOCAL HYBRID SYNTHETIC FILE\nline two\n'
        upload = { 'name': 'synthetic.txt', 'content_base64': base64.b64encode(raw).decode(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'request_id': rid, 'operation': 'plugin',
            'plugin_key': p['plugin_key'], 'plugin_version': p['version'], 'explicit_upload': True }
        status, _, attachment = a.request(f'/v1/cloud/workspaces/{wid}/attachments', upload, ta)
        check(status == 200, 'CLOUD_CONFIRMED_UPLOAD_BOUND_TO_TASK')
        task = { 'request_id': rid, 'plugin_key': p['plugin_key'], 'plugin_version': p['version'],
                 'input': 'Summarize the selected synthetic file', 'attachment_ids': [attachment['attachment_id']] }
        status, _, result = a.request(f'/v1/cloud/workspaces/{wid}/tasks', task, ta)
        check(status == 200 and result['status'] == 'completed' and result['simulation'], 'CLOUD_PLUGIN_EXECUTED')
        status, _, repeat = a.request(f'/v1/cloud/workspaces/{wid}/tasks', task, ta)
        check(status == 200 and repeat['run_id'] == result['run_id'], 'CLOUD_TASK_RETRY_IDEMPOTENT')
        status, _, history = a.request('/v1/cloud/conversations/' + result['conversation_id'], token=ta)
        check(status == 200 and history['visibility'] == 'private' and len(history['runs']) == 1, 'CLOUD_PRIVATE_HISTORY')
        check(b.request('/v1/cloud/conversations/' + result['conversation_id'], token=tb,
              extra_headers={'X-Employee-No': 'LOCAL-FIXTURE-A'})[0] in (403, 404), 'CLOUD_FOREIGN_HISTORY_DENIED')
        check(b.request('/v1/cloud/attachments/' + attachment['attachment_id'], token=tb)[0] in (403, 404), 'CLOUD_FOREIGN_ATTACHMENT_DENIED')
        status, _, _ = a.request('/v1/cloud/conversations/' + result['conversation_id'] + '/share', {'visibility': 'workspace'}, ta)
        check(status == 200, 'CLOUD_OWNER_EXPLICIT_SHARE')
        check(b.request('/v1/cloud/conversations/' + result['conversation_id'], token=tb)[0] in (403, 404), 'CLOUD_SHARED_STILL_REQUIRES_DEPARTMENT_GRANT')
        a.request('/v1/cloud/conversations/' + result['conversation_id'] + '/share', {'visibility': 'private'}, ta)
        status, _, chat = a.request(f'/v1/cloud/workspaces/{wid}/chats', { 'request_id': str(uuid4()),
            'input': 'LOCAL HYBRID PERSONAL KEY CLOUD CHAT' }, ta)
        check(status == 200 and chat['status'] == 'completed'
              and 'LOCAL-FIXTURE-A' in chat['result']['text'], 'CLOUD_CHAT_OWN_PERSONAL_KEY')
        print('HYBRID_REAL_LOCAL_HTTP_CHAIN: PASS')
    finally:
        a.request(PREFIX + '/logout', {}, ta)
        b.request(PREFIX + '/logout', {}, tb)


if __name__ == '__main__':
    run()
