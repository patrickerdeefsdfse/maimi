"""Real loopback HTTP/PG tests using only an owned synthetic group and users."""
import base64
import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request
from urllib.error import HTTPError
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from desktop_auth_smoke import login, check, BASE, PREFIX


def run():
    if os.getenv('WSL_DISTRO_NAME') != 'MegmeetDev':
        raise RuntimeError('LOCAL_ENVIRONMENT_REQUIRED')
    dsn = os.environ['IAM_TEST_DATABASE_URL']
    w = str(uuid4())
    a, ga = login('a')
    b, gb = login('b')
    ta, tb = ga['access_token'], gb['access_token']

    def request(client, token, route, body=None, method=None):
        req = Request(BASE + '/v1/cloud' + route,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}, method=method)
        try:
            response = client.client.open(req, timeout=130)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    try:
        with psycopg.connect(dsn) as conn:
            conn.execute('''INSERT INTO platform.hybrid_workspaces(id,organization_code,name,kind)
                VALUES(%s,%s,'三模式验证小组','group')''', (w, 'TEST-GROUP-' + w))
            for user in ('LOCAL-FIXTURE-A', 'LOCAL-FIXTURE-B'):
                conn.execute('''INSERT INTO platform.hybrid_workspace_grants
                    (workspace_id,user_no,can_chat,can_share,can_publish,can_retrieve)
                    VALUES(%s,%s,true,true,true,true)''', (w, user))
            conn.execute('''INSERT INTO platform.hybrid_plugins
                (workspace_id,plugin_key,version,name,runner,model_policy)
                VALUES(%s,'knowledge-qa','1.0.0','团队资料问答','knowledge-qa-v1',%s)''',
                (w, Jsonb({'mode': 'actor-model'})))
            for user in ('LOCAL-FIXTURE-A', 'LOCAL-FIXTURE-B'):
                conn.execute('''INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no)
                    VALUES(%s,'knowledge-qa','1.0.0',%s)''', (w, user))

        status, caps = request(a, ta, '/capabilities')
        check(status == 200 and caps['version'] == 2, 'WORKSPACES_V2_CAPABILITY')
        for client, token in ((a, ta), (b, tb)):
            status, spaces = request(client, token, '/workspaces')
            check(status == 200 and any(x['workspace_id'] == w and x['kind'] == 'group' for x in spaces['items']), 'GROUP_EXPLICIT_MEMBERSHIP')
        doc = {'name': 'synthetic-team-notes.txt', 'content': 'WORKSPACE_V2 reference: blue connector specification.',
               'visibility': 'workspace', 'explicit_upload': True}
        status, first = request(a, ta, f'/workspaces/{w}/documents', doc)
        check(status == 200 and first['version'] == 1, 'CLOUD_DOCUMENT_PUBLISHED_V1')
        did = first['document_id']
        status, _ = request(b, tb, '/documents/' + did + '?version=1')
        check(status == 200, 'AUTHORIZED_GROUP_MEMBER_READ')
        private = {**doc, 'name': 'private.txt', 'visibility': 'private', 'content': 'UNIQUE_PRIVATE_SOURCE_ONLY'}
        status, private_doc = request(a, ta, f'/workspaces/{w}/documents', private)
        check(status == 200 and request(b, tb, '/documents/' + private_doc['document_id'])[0] == 404, 'PRIVATE_DOCUMENT_HIDDEN')
        status, search = request(b, tb, f'/workspaces/{w}/retrieve', {'query': 'UNIQUE_PRIVATE_SOURCE_ONLY'})
        check(status == 200 and not search['items'], 'RETRIEVAL_FILTERS_PRIVATE_SOURCE')
        status, second = request(a, ta, '/documents/' + did, {**doc, 'content': doc['content'] + '\nrevision two', 'expected_version': 1}, 'PUT')
        check(status == 200 and second['version'] == 2, 'CLOUD_DOCUMENT_UPDATED_V2')
        check(request(a, ta, '/documents/' + did, {**doc, 'expected_version': 1}, 'PUT')[0] == 409, 'STALE_EDIT_CONFLICT')
        check(request(a, ta, '/documents/' + did + '?version=1')[1]['content'] == doc['content'], 'OLD_DOCUMENT_VERSION_IMMUTABLE')
        rid = str(uuid4())
        data = b'EXPLICIT_LOCAL_FILE: green connector comparison'
        status, receipt = request(a, ta, f'/workspaces/{w}/attachments', {
            'name': 'local-selected.txt', 'content_base64': base64.b64encode(data).decode(),
            'sha256': hashlib.sha256(data).hexdigest(), 'request_id': rid, 'operation': 'plugin',
            'plugin_key': 'knowledge-qa', 'plugin_version': '1.0.0', 'explicit_upload': True})
        check(status == 200, 'EXPLICIT_LOCAL_FILE_UPLOADED')
        task = {'request_id': rid, 'plugin_key': 'knowledge-qa', 'plugin_version': '1.0.0',
                'input': 'Compare selected sources.', 'attachment_ids': [receipt['attachment_id']],
                'document_refs': [{'document_id': did, 'version': 1}], 'explicit_cloud_context': True}
        check(request(a, ta, f'/workspaces/{w}/tasks', {**task, 'explicit_cloud_context': False})[0] in (400,422), 'CLOUD_CONTEXT_REQUIRES_CONSENT')
        status, result = request(a, ta, f'/workspaces/{w}/tasks', task)
        check(status == 200 and result['status'] == 'completed' and 'LOCAL-FIXTURE-A' in result['result']['text'], 'HYBRID_TASK_USES_ACTOR_MODEL')
        check(result['result']['sources'][0]['version'] == 1, 'TASK_PINNED_SOURCE_VERSION')
        check(request(a, ta, f'/workspaces/{w}/tasks', task)[1]['run_id'] == result['run_id'], 'HYBRID_RETRY_NO_DUPLICATE')
        cid = result['conversation_id']
        check(request(b, tb, '/conversations/' + cid)[0] == 404, 'CLOUD_CHAT_PRIVATE_DEFAULT')
        check(request(a, ta, '/conversations/' + cid + '/share', {'visibility':'workspace'})[0] == 200, 'TASK_EXPLICIT_SHARE')
        check(request(b, tb, '/conversations/' + cid)[0] == 200, 'SHARED_TASK_VISIBLE_TO_GROUP')
        check(request(a, ta, '/documents/' + did, {**doc,'expected_version':2,'visibility':'private'}, 'PUT')[0] == 200, 'SOURCE_SHARING_WITHDRAWN')
        check(request(b, tb, '/conversations/' + cid)[0] in (403,404), 'SHARED_RESULT_RECHECKS_SOURCE_ACCESS')
        check(request(a, ta, '/documents/' + did, {**doc,'expected_version':3}, 'PUT')[0] == 200, 'SYNTHETIC_SOURCE_RESHARED')
        Path('evidence/workspaces-v2-fixture.json').write_text(json.dumps({'workspace_id':w,'document_id':did,'private_document_id':private_doc['document_id']})+'\n')
        print('WORKSPACES_V2_REAL_LOCAL_HTTP: PASS', flush=True)
    finally:
        a.request(PREFIX + '/logout', {}, ta)
        b.request(PREFIX + '/logout', {}, tb)
        with psycopg.connect(dsn) as conn:
            conn.execute('UPDATE platform.hybrid_workspaces SET enabled=false WHERE id=%s', (w,))
        print('OWNED_SYNTHETIC_WORKSPACE_DISABLED: PASS', flush=True)


if __name__ == '__main__':
    run()
