"""Synthetic official MCP contracts; no company or public provider requests."""
import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
import httpx

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('r50_eta_tests'); package.__path__ = [str(ROOT/'backend/app')]
sys.modules[package.__name__] = package
spec=importlib.util.spec_from_file_location(package.__name__+'.logistics_query',ROOT/'backend/app/logistics_query.py')
q=importlib.util.module_from_spec(spec);sys.modules[spec.name]=q;spec.loader.exec_module(q)
ORDER='SDXM9307330001'; WAYBILL='DPK927365110001'

def trace():
    return {'kuaidiNum':WAYBILL,'kuaidiCom':'debangkuaidi','kuaidiName':'合成快递','state':'在途',
            'fromTo':'合成起点 -> 合成终点','expType':'来源明确的产品',
            'data':[{'time':'2026-10-09 12:00:00','status':'在途','context':'合成最新动态'},
                    {'time':'2026-10-08 10:00:00','status':'揽收','context':'合成揽收'}]}

def eta_tool():
    names=['kuaidi_com','from_loc','to_loc','order_time','exp_type','logistic']
    return {'name':'estimate_time_with_logistic','inputSchema':{'type':'object','properties':{x:{'type':'string'} for x in names},'required':[x for x in names if x!='order_time']}}

class Server:
    def __init__(self):
        self.trace=trace();self.tool=eta_tool();self.calls=[];self.result_patch={};self.eta_error=False;self.authorized=True
        self.mes_waybills=[WAYBILL];self.lose_authorization_on_eta=False
        self.eta_delay=0
    async def __call__(self,request):
        body=json.loads(request.content)
        if str(request.url)==q.MES_ENDPOINT:
            return httpx.Response(200,json={'code':0,'ObjectData':[{'物流单号':n} for n in self.mes_waybills]})
        method=body['method']
        if method=='notifications/initialized':return httpx.Response(202)
        if method=='initialize':result={'protocolVersion':'2025-03-26','capabilities':{'tools':{}}}
        elif method=='tools/list':
            result={'tools':[{'name':'query_trace','inputSchema':{'type':'object','properties':{'kuaidi_num':{'type':'string'}},'required':['kuaidi_num']}}] + ([self.tool] if self.tool else [])}
        elif method=='tools/call':
            params=body['params'];self.calls.append(params)
            if params['name']=='query_trace':payload={**self.trace,'kuaidiNum':params['arguments']['kuaidi_num']}
            else:
                if self.eta_delay:await asyncio.sleep(self.eta_delay)
                if self.eta_error:return httpx.Response(502)
                args=params['arguments']
                payload={'fromName':args['from_loc'],'toName':args['to_loc'],'orderTime':args['order_time'],
                         'expType':args['exp_type'],'arrivalTime':'2026-10-10 17:30:00',**self.result_patch}
                if self.lose_authorization_on_eta:self.authorized=False
            result={'isError':False,'content':[],'structuredContent':payload}
        else:raise AssertionError(method)
        return httpx.Response(200,json={'jsonrpc':'2.0','id':body['id'],'result':result})

async def query(server):
    service=q.LogisticsQueryService(q.LogisticsConfig(enabled=True,kuaidi100_mcp=q.Kuaidi100MCPConfig(key='SYNTHETIC_ETA_KEY',single_process_rate_guard=True)),transport=httpx.MockTransport(server))
    return await service.query({'query_text':ORDER},trusted_user_no='SYNTHETIC',authorize=lambda:server.authorized)

class Integration(unittest.IsolatedAsyncioTestCase):
    async def test_complete_sources_call_official_in_transit_method_and_show_real_eta(self):
        server=Server();result=await query(server)
        self.assertEqual([x['name'] for x in server.calls],['query_trace','estimate_time_with_logistic'])
        sent=server.calls[1]['arguments'];self.assertEqual(sent['order_time'],'2026-10-08 10:00:00')
        self.assertEqual(sent['exp_type'],'来源明确的产品');self.assertEqual(json.loads(sent['logistic']),server.trace['data'])
        self.assertEqual(result['counts']['eta_requests'],1)
        self.assertIn('运输中&#10;&#10;预计送达：2026-10-10 17:30&#10;&#10;合成最新动态',result['display_markdown'])
        self.assertNotIn('fromTo',json.dumps(result));self.assertNotIn('合成起点',json.dumps(result,ensure_ascii=False))

    async def test_actual_query_eta_reuses_trace_without_extra_call(self):
        server=Server();server.trace['arrivalTime']='2026-10-10 18'
        result=await query(server)
        self.assertEqual(len(server.calls),1);self.assertEqual(result['counts']['eta_requests'],0)
        self.assertIn('预计送达：2026-10-10 18时',result['display_markdown']);self.assertNotIn('18:00',result['display_markdown'])

    async def test_optional_product_echo_and_explicit_timezone_are_supported(self):
        server=Server();server.result_patch={'expType':None};server.trace['data'][1]['time']='2026-10-08T02:00:00Z'
        result=await query(server)
        self.assertEqual(server.calls[1]['arguments']['order_time'],'2026-10-08 10:00:00')
        self.assertEqual(result['rows'][0]['eta_status'],'AVAILABLE')

    async def test_missing_fields_unsupported_carrier_and_absent_tool_do_not_guess(self):
        for patch in ({'expType':None},{'expType':''},{'fromTo':'合成起点 -> '},{'fromTo':'起点 -> 中转 -> 终点'},
                      {'kuaidiCom':'huisen'},{'expType':123}):
            server=Server();server.trace.update(patch);result=await query(server)
            self.assertEqual(len(server.calls),1);self.assertNotIn('预计送达：',result['display_markdown'])
        server=Server();server.tool=None;result=await query(server);self.assertEqual(len(server.calls),1)
        self.assertEqual(result['rows'][0]['tracking_status'],'IN_TRANSIT')

    async def test_eta_failure_or_wrong_echo_preserves_successful_trace(self):
        for patch in ({'arrivalTime':'明天'},{'arrivalTime':'2026-99-12 18'},{'toName':''},
                      {'orderTime':'2026-10-07 10:00:00'},{'expType':'猜测产品'},{'orderTime':None}):
            server=Server();server.result_patch=patch;result=await query(server)
            self.assertEqual(result['rows'][0]['tracking_status'],'IN_TRANSIT')
            self.assertNotIn('预计送达：',result['display_markdown']);self.assertIn('合成最新动态',result['display_markdown'])
        server=Server();server.eta_error=True;result=await query(server)
        self.assertEqual(result['rows'][0]['tracking_status'],'IN_TRANSIT');self.assertIn('合成最新动态',result['display_markdown'])

    async def test_signed_uses_only_verified_signature_not_forecast_or_latest_reinspection(self):
        server=Server();server.trace['state']='已签收';server.trace['arrivalTime']='2026-10-10 18'
        server.trace['data'].append({'time':'2026-10-09 11:59:00','status':'签收','context':'合成签收事件'})
        result=await query(server)
        self.assertEqual(len(server.calls),1);self.assertIn('签收时间：2026-10-09 11:59',result['display_markdown'])
        self.assertNotIn('预计送达',result['display_markdown']);self.assertIn('合成最新动态',result['display_markdown'])

    async def test_unknown_eta_schema_does_not_change_tracking(self):
        server=Server();server.tool['inputSchema']['required'].append('customer_password')
        result=await query(server);self.assertEqual(len(server.calls),1);self.assertIn('合成最新动态',result['display_markdown'])

    async def test_forecast_budget_does_not_consume_or_drop_tracking(self):
        server=Server();server.mes_waybills=[f'DPK00000000{i:04d}' for i in range(6)]
        result=await query(server)
        self.assertEqual(result['counts']['tracking_requests'],6);self.assertEqual(result['counts']['eta_requests'],4)
        self.assertEqual(result['counts']['eta_budget_exceeded_rows'],2)
        self.assertEqual(len(result['display_markdown'].splitlines()),3)
        self.assertEqual(len(result['rows']),6)
        self.assertTrue(all(r['tracking_status']=='IN_TRANSIT' for r in result['rows']))
        self.assertIn('6. 运输中',result['display_markdown']);self.assertNotIn('本批时效查询已达上限',result['display_markdown'])

    async def test_authorization_lost_after_eta_never_returns_employee_result(self):
        server=Server();server.lose_authorization_on_eta=True
        with self.assertRaisesRegex(q.LogisticsQueryError,'AUTHORIZATION_LOST'):await query(server)

    async def test_overall_deadline_during_forecast_retains_already_observed_tracking(self):
        server=Server();server.eta_delay=1
        service=q.LogisticsQueryService(q.LogisticsConfig(enabled=True,total_timeout_seconds=.05,
            kuaidi100_mcp=q.Kuaidi100MCPConfig(key='SYNTHETIC_ETA_KEY',single_process_rate_guard=True)),transport=httpx.MockTransport(server))
        result=await service.query({'query_text':ORDER},trusted_user_no='SYNTHETIC',authorize=lambda:True)
        self.assertEqual(result['rows'][0]['tracking_status'],'IN_TRANSIT');self.assertIn('合成最新动态',result['display_markdown'])
        self.assertNotIn('预计送达：',result['display_markdown'])

    async def test_mismatched_trace_prevents_forecast(self):
        server=Server()
        original=server.__call__
        async def handler(request):
            response=await original(request)
            body=json.loads(request.content)
            if body.get('method')=='tools/call' and body['params']['name']=='query_trace':
                value=json.loads(response.content);value['result']['structuredContent']['kuaidiNum']='DIFFERENT12345'
                return httpx.Response(200,json=value)
            return response
        service=q.LogisticsQueryService(q.LogisticsConfig(enabled=True,kuaidi100_mcp=q.Kuaidi100MCPConfig(key='SYNTHETIC_ETA_KEY',single_process_rate_guard=True)),transport=httpx.MockTransport(handler))
        result=await service.query({'query_text':ORDER},trusted_user_no='SYNTHETIC',authorize=lambda:True)
        self.assertEqual(len(server.calls),1);self.assertEqual(result['rows'][0]['tracking_status'],'PROVIDER_INVALID_RESPONSE')

class Boundaries(unittest.TestCase):
    def test_time_precision_and_bad_types(self):
        for value in (None,4,{},'tomorrow','2026-02-30 18','2026-10-10','2026-10-10 25'):
            self.assertEqual(q._eta_time(value),'—')
        self.assertEqual(q._eta_time('2026-10-10T01:15:00Z'),'2026-10-10 09:15')
        self.assertEqual(q._eta_time('2026-10-10 09:15'),'2026-10-10 09:15')
    def test_no_partial_history_or_implicit_address_product(self):
        p=trace();p['data']*=51;self.assertIsNone(q._eta_arguments(p))
        p=trace();p['data'][0]['context']='字'*64000;self.assertIsNone(q._eta_arguments(p))
        p=trace();p['data'][0]['time']='not-time';self.assertIsNone(q._eta_arguments(p))
    def test_source_markup_cannot_add_our_separator(self):
        self.assertEqual(q._markdown_cell('段一&#10;&#10;段二\u2028段三'),'段一 段二 段三')

if __name__=='__main__':unittest.main()
