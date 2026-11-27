import base64
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from order_mcp.cloud_store import CloudStore
from order_mcp.config import Settings
from order_mcp.order_batch import accept,finish
from order_mcp.service import OrderService
from test_orders import FakeOracle
from test_web_workflow import backend, source, transport

def row(po,item,**header):
    return {"业务实体":"示例制造甲有限公司","客户名称":"示例员甲测试购方","客户代号":"910001",
            "客户PO号":po,"币种":"CNY","订购项目":item,"订单数量":1,"含税单价":4,**header}

async def stage(svc,batch,index,total,name,rows,content=None):
    class Provider:
        async def extract(self,*args):
            if rows is None:
                raise ValueError("synthetic unreadable")
            return {"rows":rows}
    svc.provider=Provider()
    original=svc.settings.data_dir/name
    original.write_bytes((name if content is None else content).encode())
    drafts,_=await svc.extract_drafts(original,name,"M90000001","QA",allow_blank=True)
    accept(svc.store,batch,index,total,name,drafts)

def fixture(tmp_path):
    sid=str(uuid4())
    store=CloudStore(tmp_path,"M90000001",sid)
    service=OrderService(Settings(data_dir=tmp_path,master_data=tmp_path/"empty"),store=store,oracle=FakeOracle())
    return service,str(uuid4())

async def test_content_groups_across_files_no_twenty_order_limit(tmp_path):
    svc,batch=fixture(tmp_path)
    await stage(svc,batch,0,2,"a.pdf",[row("PO-1","part-a")]+[row("other-"+str(i),"only") for i in range(21)])
    await stage(svc,batch,1,2,"b.xlsx",[row("PO-1","part-b")])
    with ThreadPoolExecutor(3) as pool:
        receipts=list(pool.map(lambda _:finish(svc.store,batch,[1]),range(3)))
    assert len(receipts[0]['order_ids'])==22 and receipts==[receipts[0]]*3
    first=svc.review(receipts[0]['order_ids'][0])
    assert first['source_names']==['a.pdf','b.xlsx']
    assert [item['customer_item'] for item in first['order']['lines']]==['part-a','part-b']
    assert all(item['item_code'] is None for item in first['order']['lines'])
    assert [item['file_name'] for item in first['sources']]==first['source_names']
    assert 'originals' not in first and 'original_path' not in first
    assert 'final' not in first and first['status']=='review'
    assert svc.oracle.calls==0
    assert accept(svc.store,batch,1,2,'b.xlsx') is True
    with pytest.raises(ValueError):accept(svc.store,batch,1,2,'changed')
    with pytest.raises(ValueError):finish(CloudStore(tmp_path,'M90000002',svc.store.scope[1]),batch,[])

async def test_conflict_not_overwritten_or_confirmable_until_human_edit(tmp_path):
    svc,batch=fixture(tmp_path)
    await stage(svc,batch,0,2,'a.pdf',[row('X','a',头备注='FOB')])
    await stage(svc,batch,1,2,'b.pdf',[row('X','b',头备注='CIF')])
    rid,=finish(svc.store,batch,[])['order_ids']
    view=svc.review(rid)
    assert view['order']['head_remark'] is None and view['can_prepare'] is False
    issue=next(i for i in view['issues'] if i['code']=='source_conflict')
    assert all(x in issue['message'] for x in ('FOB','CIF')) and issue['path']=='head_remark'
    with pytest.raises(ValueError):svc.prepare_submission(rid,view['revision'])
    guessed=svc.revise(rid,view['revision'],{'head_remark':'CIF'},'model',human=False)
    assert not guessed['can_prepare']
    fixed=svc.revise(rid,guessed['revision'],{'head_remark':'CIF','lines.0.item_code':'RA','lines.1.item_code':'RB'},'checked originals',human=True)
    assert 'head_remark' not in fixed['header_conflicts'] and fixed['can_prepare']

async def test_empty_failed_and_missing_po_are_reviewable_separately(tmp_path):
    svc,batch=fixture(tmp_path)
    for i,rows in enumerate(([],None,[row(None,'A'),row(None,'B')])):
        await stage(svc,batch,i,4,str(i)+'.pdf',rows)
    with pytest.raises(ValueError): finish(svc.store,batch,[])
    result=finish(svc.store,batch,[3])
    assert result['missing_indices']==[3] and len(result['order_ids'])==4
    views=[svc.review(rid) for rid in result['order_ids']]
    assert all(v['status']=='review' and not v['can_prepare'] and len(v['order']['lines'])==1 for v in views)
    assert '自动识别失败' in ''.join(views[1]['warnings'])

async def test_duplicate_bytes_keep_originals_and_distinct_customer_isolated(tmp_path):
    svc,batch=fixture(tmp_path)
    await stage(svc,batch,0,3,'same.pdf',[row('PO','part-a')],'same')
    await stage(svc,batch,1,3,'same-again.pdf',[row('PO','part-a')],'same')
    await stage(svc,batch,2,3,'other.pdf',[row('PO','part-b',客户名称='其他客户')])
    result=finish(svc.store,batch,[])
    first,other=[svc.review(rid) for rid in result['order_ids']]
    assert first['source_names']==['same.pdf','same-again.pdf'] and len(first['order']['lines'])==1
    assert other['source_names']==['other.pdf']

@pytest.mark.parametrize("accounts",[(None,"001","002"),("001",None,"002"),("001","002",None)])
async def test_partial_identity_never_attaches_to_first_competing_customer_account(tmp_path,accounts):
    svc,batch=fixture(tmp_path)
    for i,account in enumerate(accounts):
        await stage(svc,batch,i,3,f"source-{i}.pdf",[row("shared",f"line-{i}",客户代号=account)])
    views=[svc.review(rid) for rid in finish(svc.store,batch,[])["order_ids"]]
    assert len(views)==3
    assert all(len(v["source_names"])==1 and len(v["order"]["lines"])==1 for v in views)
    assert all("不同客户代号或公司主体" in "".join(v["warnings"]) for v in views)
    assert "已单独保留" in "".join(views[accounts.index(None)]["warnings"])

async def test_stateless_mcp_stage_finalize_and_confirm_boundary(backend):
    from maimi_projects import sales_orders
    db,*_=backend
    caller={'employee':'M90000001','source':'model','conversation':str(uuid4()),'display_name':'测试员工'}
    bid=str(uuid4())
    files={}
    async def read(ctx,ref,max_bytes):
        return files[ref]
    async def save(ctx,card):
        return {'status':card['status'],'project_result':card}
    async def rpc(action,arguments):
        return await transport.rpc(action,arguments,**caller)
    context=SimpleNamespace(source="model")
    bridge=SimpleNamespace(files=SimpleNamespace(read=read,record=AsyncMock(return_value=({},'native_message'))),
                           sales_orders=rpc,_authorize=AsyncMock(),_save_result=save)
    for i,fmt in enumerate(('png','pdf')):
        ref='pjf_'+str(i)*32
        fixture=source(fmt)
        files[ref]={'name':fixture['name'],'data':base64.b64decode(fixture['data'])}
        request=dict(batch_id=bid,index=i,count=2,attachment_id=ref)
        response=await sales_orders.invoke(bridge,context,'stage_file',request)
        assert response['order_result']=={'staged':True,'index':i}
    completed=await sales_orders.invoke(bridge,context,'finish_batch',{'batch_id':bid,'skipped':[1]})
    res=completed['order_result']
    rid,=res['order_ids']
    assert res['missing_indices']==[]
    view=await transport.rpc('review',{'order_id':rid},**caller)
    assert len(view['order']['lines'])==2 and db.calls==0
    form=(await sales_orders.invoke(bridge,context,'review',{'order_id':rid}))['project_result']
    assert form['sections']==form['artifacts']==[]
    assert form['order_editors'][0]['source_names']==['synthetic.png','synthetic.pdf']
    assert [a['action'] for a in form['actions']]==['confirm_order']
    with pytest.raises(transport.OrderCallFailed):
        await transport.rpc('prepare',{'order_id':rid,'revision':view['revision']},**caller)
