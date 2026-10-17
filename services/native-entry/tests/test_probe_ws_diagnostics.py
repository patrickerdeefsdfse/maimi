"""Offline WebSocket diagnostics: status accuracy, redaction and failure retention."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatusCode

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
SPEC = importlib.util.spec_from_file_location('ws_diagnostic_probe', SOURCE / 'probe_client.py')
probe = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


class WebSocketDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('EVENTS', 'WS_DIAGNOSTICS', 'HTTP_EVENTS'):
            patch = mock.patch.object(probe, name, self.root / (name + '.jsonl'))
            patch.start()
            self.addCleanup(patch.stop)
        self.trace = 'c' * 32
        self.binding = SimpleNamespace(user_id='owner-a', upstream='http://synthetic-host-a:3084')
        self.ready = {'cookie': 'dsh_token=PRIVATE_COOKIE_VALUE', 'generation': 'generation-a'}
        self.kwargs = {'host': 'synthetic-host-a', 'port': 3084,
                       'origin': 'http://127.0.0.1:3085',
                       'extra_headers': {'Cookie': self.ready['cookie']}}
        patch = mock.patch.object(probe, 'read_ready', return_value=self.ready)
        patch.start()
        self.addCleanup(patch.stop)

    def connector(self, actual):
        return probe.make_recorded_connect(SimpleNamespace(bindings=[self.binding]), actual)

    def diagnostic_events(self):
        return [json.loads(line) for line in probe.WS_DIAGNOSTICS.read_text().splitlines()]

    async def test_failed_upstream_has_status_but_no_route_success(self):
        error = InvalidStatusCode(400, Headers({'Set-Cookie': 'PRIVATE_COOKIE_VALUE'}))

        @asynccontextmanager
        async def actual(*args, **kwargs):
            self.assertFalse(probe.EVENTS.exists())
            raise error
            yield  # pragma: no cover

        token = probe.TRACE.set(self.trace)
        try:
            with self.assertRaises(InvalidStatusCode) as caught:
                async with self.connector(actual)('ws://127.0.0.1:3085/api/remote.mux', **self.kwargs):
                    self.fail('failed handshake must not yield')
            self.assertIs(caught.exception, error)
        finally:
            probe.TRACE.reset(token)
        self.assertFalse(probe.EVENTS.exists())
        events = self.diagnostic_events()
        self.assertEqual([item['event'] for item in events], ['ENTER', 'ATTEMPT', 'FAIL'])
        self.assertEqual(events[-1]['status'], 400)
        self.assertEqual(events[-1]['error'], 'InvalidStatusCode')
        self.assertNotIn('PRIVATE_COOKIE_VALUE', probe.WS_DIAGNOSTICS.read_text())

    async def test_route_success_is_recorded_only_after_actual_handshake(self):
        closed = []

        @asynccontextmanager
        async def actual(*args, **kwargs):
            self.assertFalse(probe.EVENTS.exists())
            yield SimpleNamespace(open=True)
            closed.append(True)

        token = probe.TRACE.set(self.trace)
        try:
            async with self.connector(actual)('ws://127.0.0.1:3085/api/remote.mux', **self.kwargs):
                route = json.loads(probe.EVENTS.read_text())
                self.assertEqual(route, {'owner_id': 'owner-a', 'generation': 'generation-a', 'cookie_match': True})
        finally:
            probe.TRACE.reset(token)
        self.assertEqual(closed, [True])
        self.assertEqual(self.diagnostic_events()[-1], {'trace': self.trace, 'event': 'SUCCESS', 'status': 101})

    async def test_wrong_target_still_rejected_before_actual_connector(self):
        actual = mock.Mock()
        token = probe.TRACE.set(self.trace)
        try:
            with self.assertRaisesRegex(probe.Failure, '^WS_FIXED_TARGET$'):
                async with self.connector(actual)('ws://127.0.0.1:3085/api/remote.mux',
                                                 **{**self.kwargs, 'host': 'foreign-host'}):
                    self.fail('wrong route accepted')
        finally:
            probe.TRACE.reset(token)
        actual.assert_not_called()
        self.assertFalse(probe.EVENTS.exists())
        self.assertEqual([e['event'] for e in self.diagnostic_events()], ['ENTER'])

    async def test_post_handshake_error_does_not_reclassify_handshake(self):
        @asynccontextmanager
        async def actual(*args, **kwargs):
            yield SimpleNamespace(open=True)

        token = probe.TRACE.set(self.trace)
        try:
            with self.assertRaisesRegex(ValueError, 'private stream error'):
                async with self.connector(actual)('ws://127.0.0.1:3085/api/remote.mux', **self.kwargs):
                    raise ValueError('private stream error')
        finally:
            probe.TRACE.reset(token)
        self.assertEqual(self.diagnostic_events()[-1]['event'], 'SUCCESS')
        self.assertNotIn('private stream error', probe.WS_DIAGNOSTICS.read_text())

    async def test_direct_success_never_overrides_failed_browser_handshake(self):
        calls = []

        @asynccontextmanager
        async def connect(uri, **kwargs):
            calls.append((uri, kwargs))
            if len(calls) == 1:
                token = probe.TRACE.set(kwargs['extra_headers']['X-Offline-Probe-Trace'])
                try:
                    probe.ws_event('ATTEMPT')
                    probe.ws_event('FAIL', status=400, error='InvalidStatusCode')
                finally:
                    probe.TRACE.reset(token)
                raise InvalidStatusCode(403, Headers({'Set-Cookie': 'PRIVATE_COOKIE_VALUE'}))
            yield SimpleNamespace(open=True)

        output = io.StringIO()
        with mock.patch.object(probe, 'connect', connect), redirect_stdout(output):
            with self.assertRaisesRegex(probe.Failure, '^WS_BROWSER_HANDSHAKE$'):
                await probe.websocket_check({'cookie': 'PRIVATE_SSO_COOKIE'}, self.binding, self.ready,
                                            case='INITIAL_B', forged=True)
        text = output.getvalue()
        for expected in ('PROBE_WS_DIAGNOSTIC_CASE: INITIAL_B',
                         'PROBE_WS_BROWSER_HTTP_STATUS: 403',
                         'PROBE_WS_DIAGNOSTIC_STAGE: UPSTREAM_FAILED',
                         'PROBE_WS_UPSTREAM_HTTP_STATUS: 400',
                         'PROBE_WS_DIRECT_HTTP_STATUS: 101',
                         'PROBE_WS_PRIMARY_RESULT: FAIL_PRESERVED'):
            self.assertIn(expected, text)
        for forbidden in ('PRIVATE', 'synthetic-host', 'Set-Cookie', 'http://', 'ws://', ': PASS'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(calls[0][1]['origin'], probe.PUBLIC)
        self.assertEqual(calls[1][0], 'ws://127.0.0.1:3085/api/remote.mux')
        self.assertEqual(calls[1][1]['host'], 'synthetic-host-a')
        self.assertEqual(calls[1][1]['port'], 3084)
        self.assertEqual(calls[1][1]['origin'], 'http://127.0.0.1:3085')
        self.assertEqual(calls[1][1]['extra_headers'], {'Cookie': self.ready['cookie']})

    async def test_broken_diagnostic_still_retains_primary_failure(self):
        @asynccontextmanager
        async def connect(*args, **kwargs):
            raise InvalidStatusCode(403, Headers())
            yield  # pragma: no cover

        output = io.StringIO()
        with mock.patch.object(probe, 'connect', connect), \
             mock.patch.object(probe, 'diagnose_ws_failure', side_effect=ValueError('PRIVATE')), \
             redirect_stdout(output):
            with self.assertRaisesRegex(probe.Failure, '^WS_BROWSER_HANDSHAKE$'):
                await probe.websocket_check({'cookie': 'secret'}, self.binding, self.ready, case='INITIAL_A')
        self.assertIn('PROBE_WS_PRIMARY_RESULT: FAIL_PRESERVED', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())

    async def test_successful_browser_still_requires_exact_identity_route(self):
        for owner, expected_failure in [('owner-a', False), ('owner-b', True)]:
            @asynccontextmanager
            async def connect(*args, **kwargs):
                with probe.EVENTS.open('a') as output:
                    output.write(json.dumps({'owner_id': owner, 'generation': 'generation-a',
                                             'cookie_match': True}) + '\n')
                yield SimpleNamespace(open=True)

            with mock.patch.object(probe, 'connect', connect):
                if expected_failure:
                    with self.assertRaisesRegex(probe.Failure, '^WS_IDENTITY_ROUTE$'):
                        await probe.websocket_check({'cookie': 'secret'}, self.binding, self.ready,
                                                    case='RESTORED_A')
                else:
                    await probe.websocket_check({'cookie': 'secret'}, self.binding, self.ready,
                                                case='RESTORED_A')

    async def test_denied_browser_must_not_reach_even_a_failing_connector(self):
        @asynccontextmanager
        async def connect(*args, **kwargs):
            token = probe.TRACE.set(kwargs['extra_headers']['X-Offline-Probe-Trace'])
            try:
                probe.ws_event('ENTER')
                probe.ws_event('FAIL', status=401, error='InvalidStatusCode')
            finally:
                probe.TRACE.reset(token)
            raise InvalidStatusCode(403, Headers())
            yield  # pragma: no cover

        with mock.patch.object(probe, 'connect', connect):
            with self.assertRaisesRegex(probe.Failure, '^UNAUTHORIZED_WS_REACHED_CONNECTOR$'):
                await probe.denied_websocket({'X-User-ID': 'forged-owner'})
        self.assertFalse(probe.EVENTS.exists())

    def test_summary_filters_other_traces_unknown_classes_and_raw_fields(self):
        probe.WS_DIAGNOSTICS.write_text('\n'.join(json.dumps(item) for item in (
            {'trace': 'd' * 32, 'event': 'SUCCESS', 'status': 101},
            {'trace': self.trace, 'event': 'FAIL', 'status': 401, 'error': 'PRIVATE',
             'body': 'PRIVATE', 'cookie': 'PRIVATE'})))
        output = io.StringIO()
        with redirect_stdout(output):
            probe.summarize_ws_trace(self.trace)
        self.assertIn('PROBE_WS_UPSTREAM_HTTP_STATUS: 401', output.getvalue())
        self.assertNotIn('101', output.getvalue())
        self.assertNotIn('PRIVATE', output.getvalue())

    def test_status_and_class_are_closed_safe_values(self):
        for invalid in ('403', True, 99, 600, None):
            self.assertIsNone(probe.ws_error_status(SimpleNamespace(status_code=invalid)))
        self.assertEqual(probe.ws_error_status(SimpleNamespace(response=SimpleNamespace(status_code=401))), 401)
        self.assertEqual(probe.safe_ws_error(ValueError('PRIVATE')), 'OTHER_ERROR')

    async def test_websocket_trace_survives_await_and_resets_after_request(self):
        async def application(scope, receive, send):
            self.assertEqual(probe.TRACE.get(), self.trace)
            await asyncio.sleep(0)
            probe.ws_event('ATTEMPT')
            await send({'type': 'websocket.close', 'code': 1008})

        async def noop(*args):
            return None

        outer = probe.TRACE.set('outer')
        try:
            await probe.TracedASGI(application)(
                {'type': 'websocket', 'headers': [(b'x-offline-probe-trace', self.trace.encode())]}, noop, noop)
            self.assertEqual(probe.TRACE.get(), 'outer')
        finally:
            probe.TRACE.reset(outer)
        self.assertEqual(self.diagnostic_events(), [{'trace': self.trace, 'event': 'ATTEMPT'}])


if __name__ == '__main__':
    unittest.main()
