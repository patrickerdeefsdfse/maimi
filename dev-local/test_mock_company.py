from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import mock_company


class MockContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), mock_company.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.server.server_address[1])
        cls.client = build_opener(ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(2)

    def request(self, path, headers=None, body=None, method='POST'):
        request = Request(self.base + path, headers=headers or {}, method=method,
                          data=None if body is None else json.dumps(body).encode())
        try:
            response = self.client.open(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_anonymous_and_arbitrary_cookie_denied(self):
        for cookie in ('', 'local_fixture_user=unknown', 'user_no=LOCAL-FIXTURE-A'):
            self.assertEqual(self.request('/v1/auth/query-user', {'Cookie': cookie})[0], 401)

    def test_two_distinct_synthetic_identities(self):
        identities = []
        for user in ('a', 'b'):
            status, result = self.request('/v1/auth/query-user', {'Cookie': 'local_fixture_user=' + user})
            self.assertEqual(status, 200)
            self.assertEqual(result['code'], 0)
            identities.append(result['data']['userNo'])
        self.assertNotEqual(*identities)

    def test_model_authorization_and_model_name(self):
        body = {'model': mock_company.MODEL, 'messages': [{'role': 'user', 'content': 'test'}]}
        self.assertEqual(self.request('/v1/chat/completions', body=body)[0], 401)
        headers = {'Authorization': 'Bearer ' + mock_company.MODEL_KEY}
        status, result = self.request('/v1/chat/completions', headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(result['choices'][0]['message']['content'], 'LOCAL_MODEL_OK: test')
        body['model'] = 'unapproved-model'
        self.assertEqual(self.request('/v1/chat/completions', headers, body)[0], 400)

    def test_no_unimplemented_company_contract(self):
        self.assertEqual(self.request('/v1/auth/refresh-token')[0], 404)
        self.assertEqual(self.request('/v1/auth/logout')[0], 404)
        self.assertEqual(self.request('/v1/endpoint/verify-access')[0], 403)

    def test_native_stream_uses_latest_user_and_terminates(self):
        body = {'model': mock_company.MODEL, 'stream': True, 'messages': [
            {'role': 'user', 'content': 'first'},
            {'role': 'assistant', 'content': 'prior answer'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'second'}]},
            {'role': 'user', 'content': 'Current runtime context. Synthetic native state'},
            {'role': 'system', 'content': 'updated native harness context'},
        ]}
        req = Request(self.base + '/v1/chat/completions', data=json.dumps(body).encode(),
                      headers={'Authorization': 'Bearer ' + mock_company.MODEL_KEY,
                               'Content-Type': 'application/json'})
        with self.client.open(req, timeout=3) as response:
            self.assertIn('text/event-stream', response.headers['Content-Type'])
            events = response.read().decode().strip().split('\n\n')
        self.assertEqual(events[-1], 'data: [DONE]')
        chunks = [json.loads(s.removeprefix('data: ')) for s in events[:-1]]
        self.assertEqual(chunks[1]['choices'][0]['delta']['content'], 'LOCAL_MODEL_OK: second')
        self.assertEqual(chunks[-1]['choices'][0]['finish_reason'], 'stop')

    def test_health_explicitly_synthetic(self):
        status, result = self.request('/health', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual(result['mode'], 'synthetic-local-only')

    def test_personal_native_tool_fixture_is_narrow_and_stops_after_result(self):
        owner='LOCAL-FIXTURE-A';file_id='a'*32
        headers={'Authorization':'Bearer '+mock_company.PERSONAL_MODEL_KEYS[owner]}
        body={'model':mock_company.PERSONAL_MODELS[owner][0],
              'messages':[{'role':'user','content':'CLOUD_TOOL_WRITE_'+file_id}],
              'tools':[{'type':'function','function':{'name':'write'}}]}
        status,result=self.request('/personal-model/v1/chat/completions',headers,body)
        self.assertEqual(status,200)
        call=result['choices'][0]['message']['tool_calls'][0]
        self.assertEqual(json.loads(call['function']['arguments']),
            {'file_path':'/workspace/native-tool-'+file_id+'.txt','content':'SYNTHETIC_NATIVE_TOOL_'+owner})
        body['messages'].append({'role':'tool','tool_call_id':call['id'],'content':'created'})
        status,result=self.request('/personal-model/v1/chat/completions',headers,body)
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')
        body['messages']=[{'role':'user','content':'CLOUD_TOOL_WRITE_../../outside'}]
        status,result=self.request('/personal-model/v1/chat/completions',headers,body)
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')


if __name__ == '__main__':
    unittest.main()
