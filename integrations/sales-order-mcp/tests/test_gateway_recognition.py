"""Gateway catalog action -> configured FastAPI provider -> review/preview boundary.

Uses real authenticated stateless HTTP MCP. Image extraction and Oracle are
synthetic, so no company service, employee data or business order is touched.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import socket
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn

from order_mcp.cloud import create_app
from order_mcp.config import Settings
from order_mcp.fastapi_extract import FastAPIExtractor, TEST, PRODUCTION
from test_orders import FakeOracle, FakeApis
from test_web_workflow import source
from app import sales_order_transport as transport
from maimi_projects.service import Context, ProjectFailure, ProjectSkillService


@pytest.fixture
async def configured_backend(tmp_path, monkeypatch, request):
    db,api,seen=FakeOracle(),FakeApis(),[]
    count, allow_multiline = request.param
    monkeypatch.setenv('ORDER_EXTRACT_URL',TEST+'/noauth')
    monkeypatch.delenv('ORDER_EXTRACT_PLATFORM_TOKEN',raising=False)
    async def extract(self,name,raw,employee,display_name):
        self.check_config()
        seen.append({'base':self.base,'employee':employee,'name':display_name})
        header={'业务实体':'示例制造甲有限公司','客户名称':'示例员甲测试购方','客户代号':'910001',
                '客户PO号':'TEST-DIRECT-THREE','含税单价':3}
        rows=[header|{'订购项目':'TEST-'+str(i),'订单数量':i} for i in range(1, count+1)]
        rows[0]['订单数量'] = '待核原件'
        return {'rows':rows,'workbook':b'synthetic-xlsx','workbook_kind':'xlsx'}
    monkeypatch.setattr(FastAPIExtractor,'extract',extract)
    master=tmp_path/'master.json'
    master.write_text(json.dumps({"items":[{"customer_item":f"TEST-{i}","item_code":f"R000{i}","account_number":"910001"} for i in range(1,count+1)]}))
    settings=Settings(data_dir=tmp_path,master_data=master,enable_write=True,
                      multi_line_confirmed=allow_multiline,integration_config=tmp_path/'empty')
    secret='synthetic-recognition-only-'*3
    app=create_app(settings,auth_key=secret,oracle=db,archive_apis=api)
    sock=socket.socket();sock.bind(('127.0.0.1',0))
    monkeypatch.setenv('MAIMI_ORDER_MCP_SECRET',secret)
    monkeypatch.setenv('MAIMI_ORDER_MCP_URL',f'http://127.0.0.1:{sock.getsockname()[1]}/mcp')
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
    runner=asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if runner.done():await runner
                await asyncio.sleep(.01)
        yield db,api,seen,count,allow_multiline
    finally:
        server.should_exit=True
        await runner
        sock.close()


@pytest.mark.parametrize("configured_backend", [(3, True), (3, False), (1, False)], indirect=True)
async def test_catalog_recognize_reaches_review_and_human_preview_without_mode_loop(configured_backend,monkeypatch):
    db,api,seen,count,allow_multiline=configured_backend
    # Grant SQL is separately exercised in the real gateway runtime tests.
    # This image carries the order MCP runtime, not the gateway's FastAPI stack.
    monkeypatch.setitem(sys.modules,'app.plugin_admin',SimpleNamespace(
        project_granted=lambda employee,plugin:True,
        inventory=lambda employee:[{'plugin_key':'sales-orders','effective':True}]))
    saved={};reads=[];written=[]
    ctx=Context('M90000005','session-'+str(uuid4()),'cloud',('sales-orders',),'offline-qa')
    raw=source('png')
    class State:
        async def get(self,c,key):return saved.get((c.user_no,c.native_session_id,key))
        async def put(self,c,key,value):saved[c.user_no,c.native_session_id,key]=value
        @asynccontextmanager
        async def lock(self,c):yield
    class Files:
        async def record(self,aid):return {},'native_message'
        async def read(self,c,aid,maximum):
            assert c.user_no==ctx.user_no and c.native_session_id==ctx.native_session_id
            reads.append(aid)
            return {'name':raw['name'],'data':base64.b64decode(raw['data'])}
        async def write(self,c,name,data,media_type):
            written.append((name,media_type,data))
            return {'attachment_id':'qa-output-'+str(len(written)),'name':name}
    async def authorize(c):assert c.user_no==ctx.user_no
    state,files=State(),Files()
    def service(c):
        async def call(action,arguments):
            return await transport.rpc(action,arguments,source=c.source,employee=c.user_no,
                                       conversation=c.native_session_id,display_name='测试员工')
        return ProjectSkillService(authorize=authorize,state=state,files=files,sales_order_call=call)
    svc=service(ctx)
    catalog=await svc.catalog(ctx)
    actions={x['name'] for row in catalog for x in row['tools']}
    assert 'recognize' in actions and not actions & {'recognize_test','recognize_production','submit','confirm_order'}
    status=await svc.invoke(ctx,'sales-orders','status',{})
    assert not status['order_result']['approval_configured'] and not status['order_result']['archive_configured']
    assert status['order_result']['recognition']['environment']=='test'
    assert status['order_result']['recognition']['configured']
    result=await svc.invoke(ctx,'sales-orders','recognize',{'attachment_id':'qa-png'})
    assert len(seen)==1 and seen[0]['base']==TEST+'/noauth' and seen[0]['employee']==ctx.user_no
    assert reads==['qa-png'] and not {'recognize_test','recognize_production'} & {a['action'] for a in result['project_result']['actions']}
    view=result['order_result']['orders'][0]
    assert len(view['order']['lines'])==count
    assert result['status']=='needs_input' and [a['action'] for a in result['project_result']['actions']]==['confirm_order']
    assert '结束本轮' in result['next_step']
    notices = [s['text'] for s in result['project_result']['sections'] if s['type']=='notice']
    assert any('订购数量' in n for n in notices)
    editor, = result['project_result']['order_editors']
    assert editor['order_id'] == view['id'] and editor['revision'] == view['revision']
    assert editor['defaults']['currency']['value'] == 'CNY' and 'lines.0.request_date' in editor['defaults']
    assert editor['observations']['lines.0.quantity'] == '待核原件' and 'lines.0.quantity' not in editor['confidence']
    blocked = count>1 and not allow_multiline
    assert any('尚未启用多行建单' in n for n in notices) == blocked
    db.price = lambda *args: pytest.fail("recognized unit price must skip Oracle pricing")
    lookup = await svc.invoke(ctx,'sales-orders','lookup_price',{'order_id':view['id'],'line_index':0})
    assert lookup['order_result']['price_present'] and lookup['order_result']['candidate'] is None
    assert lookup['project_result']['summary'] == '第 1 行沿用已有单价 3（含税）。'
    assert lookup['project_result']['status'] == 'completed' and 'next_step' not in lookup
    resumed = await svc.invoke(ctx,'sales-orders','review',{'order_id':view['id']})
    saved_view = resumed['order_result']
    changes = {'lines.0.quantity':'4','currency':'CNY'} | {f'lines.{i}.request_date':'2026-12-30' for i in range(count)}
    revised = await svc.invoke(ctx,'sales-orders','revise',{'order_id':saved_view['id'],'revision':saved_view['revision'],
                                   'changes':changes,'reason':'示例员甲测试补充已核对字段'})
    view = revised['order_result']
    assert 'lines.0.quantity' not in revised['project_result']['order_editors'][0]['observations']
    assert any(a['action']=='confirm_order' for a in revised['project_result']['actions'])
    with pytest.raises(ProjectFailure):
        await svc.invoke(ctx,'sales-orders','confirm_order',{'order_id':view['id'],'revision':view['revision']})
    assert any(name.endswith('.xlsx') for name,_,_ in written)
    with pytest.raises(ProjectFailure):
        await svc.invoke(ctx,'sales-orders','prepare',{'order_id':view['id'],'revision':view['revision']})
    human_ctx=replace(ctx,source='human')
    preview=await service(human_ctx).invoke(human_ctx,'sales-orders','prepare',{'order_id':view['id'],'revision':view['revision']})
    if blocked:
        assert not preview['project_result']['actions']
        assert '仅供复核和下载' in preview['project_result']['summary']
    else:
        confirm=next(a for a in preview['project_result']['actions'] if a['action']=='submit')
        assert confirm['label']=='确认创建 Oracle 订单'
        with pytest.raises(ProjectFailure):
            await svc.invoke(ctx,'sales-orders','submit',confirm['arguments'])
    assert 'confirmation_token' not in preview['order_result']
    assert preview['project_result']['order_editors'][0]['revision'] == preview['order_result']['revision']
    human = replace(ctx,source='human')
    reorder={'order_id':view['id'],'revision':preview['order_result']['revision'],'reason':'示例员甲复核增删',
             'changes':{'line_layout':[count-1,None],'lines.1.item_code':'QA-ADDED'}}
    with pytest.raises(ProjectFailure):
        await svc.invoke(ctx,'sales-orders','revise',reorder)
    reshaped = await service(human).invoke(human,'sales-orders','revise',reorder)
    new_editor, = reshaped['project_result']['order_editors']
    assert [r['item_code'] for r in new_editor['lines']] == ['R000'+str(count),'QA-ADDED']
    assert 'lines.1.request_date' in new_editor['defaults']
    stale = await service(human).invoke(human,'sales-orders','revise',reorder)
    assert stale['status']=='failed' and stale['project_result']['actions'][0]['action']=='review'
    assert any(name.endswith('.xml') for name,_,_ in written)
    assert db.calls==0 and api.uploads==0
    # The new direct path cannot accept model-chosen environments or endpoints.
    for forged in ({'environment':'production'},{'url':'https://example.invalid'}):
        with pytest.raises(ProjectFailure):
            await svc.invoke(ctx,'sales-orders','recognize',{'attachment_id':'qa-png'}|forged)
    assert len(seen)==1 and reads==['qa-png']
    # An old production button must not silently use the new test default.
    human=replace(ctx,source='human')
    legacy=await service(human).invoke(human,'sales-orders','recognize_production',{'attachment_id':'qa-png'})
    assert legacy['status']=='failed' and '平台认证' in legacy['project_result']['summary']
    assert len(seen)==1 and db.calls==0 and api.uploads==0


def test_environment_status_is_fixed_and_never_exposes_platform_token(monkeypatch):
    monkeypatch.setenv('ORDER_EXTRACT_URL',TEST+'/noauth')
    monkeypatch.delenv('ORDER_EXTRACT_PLATFORM_TOKEN',raising=False)
    configured=FastAPIExtractor.configured()
    assert configured.target_status()=={'environment':'test','configured':True,'authentication':'test_noauth','selection':'administrator_configured'}
    monkeypatch.setenv('ORDER_EXTRACT_URL',PRODUCTION)
    assert not FastAPIExtractor.configured().target_status()['configured']
    monkeypatch.setenv('ORDER_EXTRACT_PLATFORM_TOKEN','synthetic-secret-never-returned')
    status=FastAPIExtractor.configured().target_status()
    assert status['configured'] and status['environment']=='production'
    assert 'synthetic-secret-never-returned' not in repr(status)
    monkeypatch.setenv('ORDER_EXTRACT_URL','https://example.invalid')
    with pytest.raises(ValueError):FastAPIExtractor.configured()
