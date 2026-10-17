"""Actual hosted tool schema with synthetic MES/trace/ETA responses only."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('r52_hosted_eta_tests')
package.__path__ = [str(ROOT / 'backend/app')]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + '.logistics_query', ROOT / 'backend/app/logistics_query.py')
q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = q
spec.loader.exec_module(q)
ORDER = 'SDXM9307330001'
WAYBILL = 'DPK927365110001'


def hosted_tool():
    required = ['orderTime', 'logistic', 'to', 'kuaidicom', 'from']
    return {'name': 'estimate_time_with_logistic', 'inputSchema': {
        'type': 'object', 'properties': {x: {'type': 'string'} for x in
            required + ['responseFormat', 'syntheticOptional']}, 'required': required}}


class Server:
    def __init__(self):
        self.trace = {'kuaidiNum': WAYBILL, 'kuaidiCom': 'debangkuaidi',
            'kuaidiName': '合成快递', 'state': '1002', 'fromTo': '合成起点->合成终点',
            'data': [{'time': '2026-10-09 12:00:00', 'status': '干线', 'context': '合成运输动态'},
                     {'time': '2026-10-08T02:00:00Z', 'status': '已揽收', 'context': '合成揽收'}]}
        self.tool = hosted_tool()
        self.calls = []
        self.mes_rows = [{'物流单号': WAYBILL}]
        self.eta_delay = 0
        self.eta_started = asyncio.Event()
        self.authorized = True
        self.revoke_on_eta = False
        self.result_patch = {}

    async def __call__(self, request):
        body = json.loads(request.content)
        if str(request.url) == q.MES_ENDPOINT:
            return httpx.Response(200, json={'code': 0, 'ObjectData': self.mes_rows})
        method = body['method']
        if method == 'notifications/initialized':
            return httpx.Response(202)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'query_trace', 'inputSchema': {
                'type': 'object', 'properties': {x: {'type': 'string'} for x in
                    ['kuaidiNum', 'phone', 'responseFormat']}, 'required': ['kuaidiNum']}}, self.tool]}
        elif method == 'tools/call':
            params = body['params']
            self.calls.append(params)
            args = params['arguments']
            if params['name'] == 'query_trace':
                payload = {**self.trace, 'kuaidiNum': args['kuaidiNum']}
            elif params['name'] == 'estimate_time_with_logistic':
                self.eta_started.set()
                if self.eta_delay:
                    await asyncio.sleep(self.eta_delay)
                payload = {'fromName': args['from'], 'toName': args['to'],
                    'orderTime': args['orderTime'], 'arrivalTime': '2026-10-10 17:30:00',
                    'remainTime': '1', 'deliveryExpendTime': '2', **self.result_patch}
                if self.revoke_on_eta:
                    self.authorized = False
            else:
                raise AssertionError('pre-shipment or unapproved tool called')
            result = {'isError': False, 'content': [], 'structuredContent': payload}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})


async def query(server, **config):
    service = q.LogisticsQueryService(q.LogisticsConfig(enabled=True,
        kuaidi100_mcp=q.Kuaidi100MCPConfig(key='SYNTHETIC_HOSTED_KEY', single_process_rate_guard=True),
        **config), transport=httpx.MockTransport(server))
    return await service.query({'query_text': ORDER}, trusted_user_no='SYNTHETIC', authorize=lambda: server.authorized)


class HostedIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_observed_contract_without_product_uses_only_real_trace_inputs(self):
        server = Server()
        result = await query(server)
        self.assertEqual([x['name'] for x in server.calls], ['query_trace', 'estimate_time_with_logistic'])
        args = server.calls[-1]['arguments']
        self.assertEqual(set(args), {'kuaidicom', 'from', 'to', 'orderTime', 'logistic', 'responseFormat'})
        self.assertEqual(args['kuaidicom'], 'debangkuaidi')
        self.assertEqual(args['from'], '合成起点')
        self.assertEqual(args['to'], '合成终点')
        self.assertEqual(args['orderTime'], '2026-10-08 10:00:00')
        self.assertEqual(args['responseFormat'], 'json')
        history = json.loads(args['logistic'])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1], {'time': '2026-10-08 10:00:00', 'status': '已揽收', 'context': '合成揽收'})
        self.assertEqual(result['counts']['eta_requests'], 1)
        self.assertIn('运输中&#10;&#10;预计送达：2026-10-10 17:30&#10;&#10;合成运输动态', result['display_markdown'])
        for private in ('合成起点', '合成终点', 'fromTo', 'expType', 'SYNTHETIC_HOSTED_KEY'):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))

    async def test_unused_product_does_not_invent_input_or_reject_provider_product(self):
        for product in (None, '', 123, '源记录的产品', 'x' * 81):
            server = Server()
            server.trace['expType'] = product
            server.result_patch['expType'] = '上游返回的产品'
            result = await query(server)
            self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')
            self.assertFalse({'expType', 'exp_type'} & set(server.calls[-1]['arguments']))

    async def test_route_time_and_arrival_response_binding_stays_strict(self):
        for patch in ({'fromName': '广东省东莞市长安镇'}, {'toName': '安徽省合肥市庐阳区'}, {'orderTime': None},
                      {'orderTime': '2026-10-07 10:00:00'}, {'arrivalTime': '明天'}, {'arrivalTime': '2026-99-10 17'}):
            server = Server()
            server.trace['fromTo'] = '广东省深圳市南山区 -> 安徽省宣城市绩溪县'
            server.result_patch.update(patch)
            result = await query(server)
            self.assertEqual(result['rows'][0]['tracking_status'], 'IN_TRANSIT')
            self.assertEqual(result['rows'][0]['eta_status'], 'UNVERIFIED')
            self.assertNotIn('预计送达：', result['display_markdown'])
            self.assertIn('合成运输动态', result['display_markdown'])

    async def test_supplier_route_normalization_preserves_request_and_rejects_explicit_conflict(self):
        server = Server()
        server.trace['fromTo'] = '合成省, 合成市, 甲区 -> 合成省, 合成市, 乙区'
        server.result_patch.update({'fromName': ' 合成省，合成市，甲区 ', 'toName': '合成省 , 合成市 , 乙区'})
        result = await query(server)
        self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')
        self.assertEqual(server.calls[-1]['arguments']['from'], '合成省, 合成市, 甲区')
        self.assertEqual(server.calls[-1]['arguments']['to'], '合成省, 合成市, 乙区')
        for destination in (None, '', ' ', '合成省,合成市,甲区'):
            server = Server()
            server.trace['fromTo'] = '合成省, 合成市, 甲区 -> 合成省, 合成市, 乙区'
            server.result_patch['toName'] = destination
            result = await query(server)
            self.assertEqual(result['rows'][0]['eta_status'], 'UNVERIFIED')
        for destination in ('合成省合成市乙区', '合成省,合成市,乙'):
            server = Server()
            server.trace['fromTo'] = '合成省, 合成市, 甲区 -> 合成省, 合成市, 乙区'
            server.result_patch['toName'] = destination
            result = await query(server)
            self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')

    async def test_no_guessing_missing_ambiguous_route_or_unsupported_carrier(self):
        for patch in ({'fromTo': ''}, {'fromTo': '起点 -> '}, {'fromTo': '起点 -> 中转 -> 终点'},
                      {'fromTo': '合成起点至合成终点'}, {'kuaidiCom': 'huisen'}):
            server = Server()
            server.trace.update(patch)
            if patch.get('kuaidiCom') == 'huisen':
                server.mes_rows = [{'物流单号': '汇森800000000001'}]
            result = await query(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(result['counts']['eta_requests'], 0)
            self.assertNotIn('预计送达：', result['display_markdown'])

    async def test_partial_or_invalid_history_never_becomes_forecast_input(self):
        for patch in ({'time': '未提供'}, {'context': None}, {'status': 1002}):
            server = Server()
            server.trace['data'][1].update(patch)
            result = await query(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(result['counts']['eta_requests'], 0)

    async def test_signed_codes_use_signature_time_and_do_not_forecast(self):
        for state in ('3', '301', '302', '303', '304', '本人签收', '代签'):
            server = Server()
            server.trace['state'] = state
            server.trace['data'].append({'time': '2026-10-09 11:59:00', 'status': state, 'context': '合成签收'})
            result = await query(server)
            self.assertEqual(result['rows'][0]['tracking_status'], 'DELIVERED')
            self.assertEqual(result['counts']['eta_requests'], 0)
            self.assertEqual(len(server.calls), 1)
            self.assertIn('签收时间：2026-10-09 11:59', result['display_markdown'])
            self.assertNotIn('预计送达', result['display_markdown'])

    async def test_pending_pickup_exception_return_and_unknown_do_not_forecast(self):
        for state in ('501', '201', '205', '4', '6', '14', '203', '101', '102', 'OTHER'):
            server = Server()
            server.trace['state'] = state
            result = await query(server)
            self.assertEqual(len(server.calls), 1)
            self.assertEqual(result['counts']['eta_requests'], 0)
            self.assertNotEqual(result['rows'][0]['tracking_status'], 'DELIVERED')

    async def test_existing_actual_arrival_time_needs_no_second_provider_request(self):
        server = Server()
        server.trace['arrivalTime'] = '2026-10-10 18'
        result = await query(server)
        self.assertEqual(len(server.calls), 1)
        self.assertIn('预计送达：2026-10-10 18时', result['display_markdown'])

    async def test_normal_forecast_taking_more_than_two_seconds_can_finish(self):
        server = Server()
        server.eta_delay = 2.05
        result = await query(server)
        self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')

    async def test_configured_request_timeout_preserves_successful_tracking(self):
        server = Server()
        server.eta_delay = 1
        result = await query(server, request_timeout_seconds=.05)
        self.assertTrue(server.eta_started.is_set())
        self.assertEqual(result['rows'][0]['tracking_status'], 'IN_TRANSIT')
        self.assertEqual(result['rows'][0]['eta_status'], 'UNAVAILABLE')
        self.assertIn('合成运输动态', result['display_markdown'])
        self.assertNotIn('预计送达：', result['display_markdown'])

    async def test_total_deadline_still_bounds_whole_operation(self):
        server = Server()
        server.eta_delay = 1
        result = await query(server, total_timeout_seconds=.05)
        self.assertTrue(server.eta_started.is_set())
        self.assertEqual(result['rows'][0]['tracking_status'], 'IN_TRANSIT')
        self.assertIn('合成运输动态', result['display_markdown'])

    async def test_eta_budget_remains_four_for_multi_waybill_records(self):
        server = Server()
        server.mes_rows = [{'物流单号': f'DPK00000000{i:04d}'} for i in range(6)]
        result = await query(server)
        self.assertEqual(result['counts']['eta_requests'], 4)
        self.assertEqual(result['counts']['tracking_requests'], 6)
        self.assertEqual(result['counts']['eta_budget_exceeded_rows'], 2)
        self.assertEqual(len(result['rows']), 6)
        self.assertEqual(len(result['display_markdown'].splitlines()), 3)

    async def test_authorization_lost_after_forecast_never_returns_employee_result(self):
        server = Server()
        server.revoke_on_eta = True
        with self.assertRaisesRegex(q.LogisticsQueryError, 'AUTHORIZATION_LOST'):
            await query(server)

    async def test_sf_phone_suffix_stays_only_on_tracking_call(self):
        server = Server()
        server.mes_rows = [{'物流单号': 'SF927365110001', q.MES_PHONE_FIELD: '13900005678'}]
        server.trace['kuaidiCom'] = 'shunfeng'
        result = await query(server)
        self.assertEqual(result['counts']['eta_requests'], 1)
        self.assertEqual(server.calls[0]['arguments']['phone'], '5678')
        self.assertNotIn('phone', server.calls[1]['arguments'])
        wire = json.dumps(server.calls[1])
        self.assertNotIn('5678', wire)
        self.assertNotIn('13900005678', json.dumps(result))

    async def test_missing_sf_phone_still_blocks_provider_query(self):
        server = Server()
        server.mes_rows = [{'物流单号': 'SF927365110001', q.MES_PHONE_FIELD: ''}]
        result = await query(server)
        self.assertFalse(server.calls)
        self.assertEqual(result['rows'][0]['tracking_status'], 'PHONE_REQUIRED')


if __name__ == '__main__':
    unittest.main()
