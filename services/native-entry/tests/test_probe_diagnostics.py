"""Safe diagnostic output and unchanged failure semantics; no Docker/network."""
from __future__ import annotations

import contextlib
import asyncio
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import httpx

SOURCE = Path(__file__).resolve().parents[1] / 'probe_client.py'
SPEC = importlib.util.spec_from_file_location('tested_probe_client', SOURCE)
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'events.jsonl'
        self.patch = mock.patch.object(probe, 'HTTP_EVENTS', self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.trace = 'a' * 32

    def events(self, values):
        self.path.write_text('\n'.join(json.dumps({'trace': self.trace, **item}) for item in values))

    def test_trace_logs_only_closed_fields(self):
        token = probe.TRACE.set(self.trace)
        try:
            probe.http_event('SSO', 'RESPONSE', status=200)
            probe.http_event('UPSTREAM', 'ERROR', error='ConnectError')
            with self.assertRaises(probe.Failure):
                probe.http_event('UPSTREAM', 'ERROR', error='Bearer_private_token')
        finally:
            probe.TRACE.reset(token)
        text = self.path.read_text()
        self.assertNotIn('Bearer', text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_error_class_never_includes_exception_message(self):
        self.assertEqual(probe.safe_http_error(httpx.ConnectError('tk-secret https://internal.invalid')), 'ConnectError')
        self.assertEqual(probe.safe_http_error(ValueError('private cookie')), 'OTHER_ERROR')

    def test_upstream_trace_has_numeric_status_without_raw_fields(self):
        self.events([{'stage': 'SSO', 'event': 'RESPONSE', 'status': 200},
                     {'stage': 'SSO', 'event': 'RESOLVED'},
                     {'stage': 'UPSTREAM', 'event': 'RESPONSE', 'status': 403,
                      'message': 'never-print', 'cookie': 'private-cookie'}])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            probe.summarize_http_trace(self.trace)
        self.assertIn('PROBE_DIAGNOSTIC_STAGE: UPSTREAM', output.getvalue())
        self.assertIn('PROBE_UPSTREAM_HTTP_STATUS: 403', output.getvalue())
        self.assertNotIn('never-print', output.getvalue())
        self.assertNotIn('private-cookie', output.getvalue())

    def test_no_upstream_does_not_claim_confirmed_bootstrap_failure(self):
        for events, stage in [([], 'LOCAL_GATE'),
                              ([{'stage': 'SSO', 'event': 'RESPONSE', 'status': 403}], 'SSO'),
                              ([{'stage': 'SSO', 'event': 'RESOLVED'},
                                {'stage': 'BOOTSTRAP', 'event': 'READ_OK'}], 'BOOTSTRAP_OR_LOCAL_GATE')]:
            self.events(events)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                probe.summarize_http_trace(self.trace)
            self.assertIn('PROBE_DIAGNOSTIC_STAGE: ' + stage, output.getvalue())

    async def test_asgi_wrapper_passes_lifespan_and_preserves_then_restores_trace(self):
        observations = []

        async def application(scope, receive, send):
            if scope['type'] == 'lifespan':
                for expected, reply in [('lifespan.startup', 'lifespan.startup.complete'),
                                        ('lifespan.shutdown', 'lifespan.shutdown.complete')]:
                    self.assertEqual((await receive())['type'], expected)
                    await asyncio.sleep(0)
                    self.assertEqual(probe.TRACE.get(), '')
                    await send({'type': reply})
                return
            before = probe.TRACE.get()
            self.assertEqual((await receive())['type'], 'http.request')
            await asyncio.sleep(0)
            observations.append((before, probe.TRACE.get()))
            probe.http_event('SSO', 'RESPONSE', status=200)
            await asyncio.sleep(0)
            probe.http_event('UPSTREAM', 'RESPONSE', status=201)
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'ok'})

        wrapped = probe.TracedASGI(application)

        async def exchange(scope, messages):
            pending, sent = list(messages), []

            async def receive():
                return pending.pop(0)

            async def send(message):
                sent.append(message)

            await wrapped(scope, receive, send)
            self.assertFalse(pending)
            return sent

        outer = probe.TRACE.set('outer-test-context')
        try:
            lifecycle = await exchange({'type': 'lifespan'},
                                       [{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
            self.assertEqual(lifecycle, [{'type': 'lifespan.startup.complete'},
                                         {'type': 'lifespan.shutdown.complete'}])
            self.assertEqual(probe.TRACE.get(), 'outer-test-context')
            self.assertFalse(self.path.exists())
            response = await exchange({'type': 'http', 'headers': [(b'x-offline-probe-trace', self.trace.encode())]},
                                      [{'type': 'http.request', 'body': b'', 'more_body': False}])
            self.assertEqual(response, [{'type': 'http.response.start', 'status': 200, 'headers': []},
                                        {'type': 'http.response.body', 'body': b'ok'}])
            self.assertEqual(observations, [(self.trace, self.trace)])
            self.assertEqual(probe.TRACE.get(), 'outer-test-context')
            traced_bytes = self.path.read_bytes()
            await exchange({'type': 'http', 'headers': []}, [{'type': 'http.request', 'body': b''}])
            self.assertEqual(observations[-1], ('', ''))
            self.assertEqual(self.path.read_bytes(), traced_bytes)
            self.assertEqual(probe.TRACE.get(), 'outer-test-context')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                probe.summarize_http_trace(self.trace)
            self.assertIn('PROBE_SSO_HTTP_STATUS: 200', output.getvalue())
            self.assertIn('PROBE_UPSTREAM_HTTP_STATUS: 201', output.getvalue())
            self.assertIn('PROBE_DIAGNOSTIC_STAGE: UPSTREAM', output.getvalue())
        finally:
            probe.TRACE.reset(outer)

    async def test_comparisons_are_same_target_controlled_headers_and_no_output_secrets(self):
        calls = []

        class Direct:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, url, *, headers, json):
                calls.append((url, headers, json))
                return httpx.Response(403 if len(calls) == 1 else 200,
                                      json={'detail': 'do-not-print-private-response'},
                                      headers={'set-cookie': 'do-not-print-header'})

        output = io.StringIO()
        ready = {'cookie': 'dsh_session=private-cookie', 'workspace_id': 'work-a'}
        with mock.patch.object(probe.httpx, 'AsyncClient', return_value=Direct()), contextlib.redirect_stdout(output):
            await probe.diagnose_native_failure(httpx.Response(503), self.trace,
                SimpleNamespace(upstream='http://host-a:3084'), ready, 'same-rpc', 'INITIAL_A')
        self.assertEqual([item[0] for item in calls], ['http://host-a:3084/api/workspace/create'] * 3)
        self.assertTrue(all(item[2] == probe.envelope('same-rpc') for item in calls))
        self.assertNotIn('X-Forwarded-Proto', calls[0][1])
        self.assertEqual(calls[1][1]['X-Forwarded-Proto'], 'https')
        self.assertEqual(calls[2][1]['Host'], '127.0.0.1:3085')
        self.assertEqual(calls[2][1]['Origin'], 'http://127.0.0.1:3085')
        for secret in ('private-cookie', 'do-not-print', 'http://host-a'):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn('PROBE_PRIMARY_RESULT: FAIL_PRESERVED', output.getvalue())

    async def test_successful_direct_diagnostics_never_convert_primary_failure_to_pass(self):
        for diagnostic in (mock.AsyncMock(return_value=None), mock.AsyncMock(side_effect=ValueError('secret'))):
            with mock.patch.object(probe, 'diagnose_native_failure', diagnostic), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(probe.Failure, '^NATIVE_HTTP_STATUS$'):
                    await probe.require_native_workspace(httpx.Response(403), self.trace,
                        SimpleNamespace(upstream='http://host-a:3084'), {'workspace_id': 'work-a'}, 'rpc', 'INITIAL_A')


if __name__ == '__main__':
    unittest.main()
