import asyncio
from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import usage_producer as p
from app import personal_model_proxy as m
from app import office_remote as o
from app import project_platform as project
from app.desktop_auth import TrustedActor
from app.iam_oauth import IAMFailure
from app.observability.monitor import MetadataStore

A = TrustedActor('M00000001', 'synthetic-account-a', 'synthetic display never retained', (), 'synthetic-session-a')
B = replace(A, user_no='M00000002', auth_session_id='synthetic-session-b')
JOB = 'a' * 32


@pytest.fixture
def store():
    value = MetadataStore()
    sink = p.configure(value)
    yield value
    p.shutdown()
    value.close()


def events(store):
    assert p._sink.drain()
    return store.read('1h')[1]


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks, self.error = chunks, error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


class Provider:
    async def resolve(self, actor):
        return m.PersonalCredential(actor.user_no, 'synthetic-only-provider-key', time.time() + 120)


async def checked(actor):
    if not isinstance(actor, TrustedActor):
        raise IAMFailure(401, 'trusted_actor_required')
    return actor


def broker(chunks, *, stream=True, status=200, error=None):
    body = Bytes(chunks, error)

    async def upstream(request):
        return httpx.Response(status, headers={'content-type':'text/event-stream' if stream else 'application/json'}, stream=body)
    return m.PersonalModelBroker(Provider(), 'https://synthetic-model.invalid/v1',
        transport=httpx.MockTransport(upstream), actor_validator=checked), body


BODY = {'model':m.DEFAULT_MODEL, 'messages':[{'role':'user','content':'synthetic prompt never retained'}]}


def test_real_model_json_usage_is_provider_only_and_metadata_exact(store):
    reply = {'choices':[{'message':{'content':'synthetic response never retained'}}],
             'usage':{'prompt_tokens':120,'completion_tokens':24,'prompt_cache_hit_tokens':80,'prompt_cache_miss_tokens':40}}
    b, raw = broker([json.dumps(reply).encode()], stream=False)
    assert asyncio.run(b.complete(A, {**BODY,'usage':{'prompt_tokens':999999}})) == reply
    rows = events(store)
    assert len(rows) == 1 and rows[0]['owner'] == A.user_no
    assert rows[0]['status'] == 'succeeded' and rows[0]['input_tokens'] == 120 and rows[0]['output_tokens'] == 24
    assert rows[0]['cache_read_tokens'] == 80 and rows[0]['cache_write_tokens'] is None
    assert set(rows[0]) == {'event_id','owner','kind','completed','status','duration','input_tokens','output_tokens','cache_read_tokens','cache_write_tokens'}
    text = json.dumps(rows)
    assert all(x not in text for x in ('synthetic prompt','synthetic response','synthetic-only-provider-key','synthetic-session','synthetic-account'))
    assert raw.closed


@pytest.mark.parametrize('cut', [1,2,7,31,2000])
def test_sse_chunk_boundaries_and_crlf_with_real_broker(store, cut):
    source = (b'data: {"choices":[{"delta":{"content":"private response"}}]}\r\n\r\n'
              b'data: {"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":9,"prompt_tokens_details":{"cached_tokens":4}}}\r\n\r\n'
              b'data: [DONE]\r\n\r\n')
    b, raw = broker([source[i:i+cut] for i in range(0,len(source),cut)])
    async def run():
        response = await b.streaming(A, BODY)
        return b''.join([chunk async for chunk in response.body_iterator])
    assert asyncio.run(run()) == source
    row = events(store)[0]
    assert (row['status'],row['input_tokens'],row['output_tokens'],row['cache_read_tokens']) == ('succeeded',17,9,4)
    assert raw.closed


@pytest.mark.parametrize('source,status,inputs', [
    (b'data: {"choices":[]}\n\ndata: [DONE]\n\n','succeeded',None),
    (b'data: {"usage":{"prompt_tokens":3}}\n\n','unknown',3),
    (b'data: {"error":{"message":"private upstream error"}}\n\ndata: [DONE]\n\n','failed',None),
    (b'data: {malformed}\n\ndata: [DONE]\n\n','unknown',None),
    (b'data: [DONE]\n\ndata: {"usage":{"prompt_tokens":99}}\n\n','unknown',None),
])
def test_no_fake_success_or_usage_and_provider_error_bytes_preserved(store,source,status,inputs):
    b, _ = broker([source])
    async def run():
        response = await b.streaming(A, BODY)
        return b''.join([chunk async for chunk in response.body_iterator])
    assert asyncio.run(run()) == source
    row = events(store)[0]
    assert row['status'] == status and row['input_tokens'] == inputs


def test_stream_transport_error_keeps_fixed_existing_error(store):
    source = b'data: {"choices":[]}\n\n'
    b, raw = broker([source], error=httpx.ReadError('synthetic raw secret error'))
    async def run():
        response = await b.streaming(A, BODY)
        return b''.join([chunk async for chunk in response.body_iterator])
    result = asyncio.run(run())
    assert result == source + b'data: {"error":{"message":"personal_model_stream_ended"}}\n\n'
    assert events(store)[0]['status'] == 'failed' and raw.closed


def test_client_stops_after_first_byte_cancelled_and_no_parse_before_yield(store, monkeypatch):
    b, raw = broker([b'data: {"choices":[]}\n\n',b'data: [DONE]\n\n'])
    calls = []
    original = p.ModelMeter.feed
    def tracked(self, chunk):
        calls.append(True)
        return original(self,chunk)
    monkeypatch.setattr(p.ModelMeter,'feed',tracked)
    async def run():
        response = await b.streaming(A,BODY)
        assert await anext(response.body_iterator) == b'data: {"choices":[]}\n\n'
        assert calls == [] and events(store) == []
        await response.body_iterator.aclose()
    asyncio.run(run())
    assert events(store)[0]['status'] == 'cancelled' and raw.closed


def test_cancelled_provider_request_never_succeeded(store):
    class Wait(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(100)
            yield b''
    async def up(request):return httpx.Response(200,stream=Wait())
    b = m.PersonalModelBroker(Provider(),'https://synthetic-model.invalid/v1',transport=httpx.MockTransport(up),actor_validator=checked)
    async def run():
        task=asyncio.create_task(b.complete(A,BODY))
        await asyncio.sleep(.01);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    asyncio.run(run())
    assert events(store)[0]['status']=='cancelled'


def test_upstream_http_error_is_recorded_but_not_changed(store):
    b,_ = broker([b'private'],status=429)
    with pytest.raises(IAMFailure) as exc:asyncio.run(b.complete(A,BODY))
    assert exc.value.status == 429 and exc.value.code == 'personal_model_upstream_unavailable'
    assert events(store)[0]['status'] == 'failed'


def test_untrusted_input_never_emits_or_calls_provider(store):
    b,_=broker([])
    with pytest.raises(IAMFailure):asyncio.run(b.complete({'user_no':A.user_no},BODY))
    assert events(store)==[] and p.model({'user_no':A.user_no}) is None


@pytest.mark.parametrize('values',[
    {'prompt_tokens':True,'completion_tokens':-1},
    {'prompt_tokens':2**80,'completion_tokens':1.5},
    {'prompt_tokens':'100','completion_tokens':None},
])
def test_invalid_tokens_unknown_not_coerced(values):
    value=p.usage(values)
    assert value['input_tokens'] is None and value['output_tokens'] is None


def test_bounded_sse_and_cache_inconsistency():
    parser=p.SSEUsage()
    for _ in range(30):parser.feed(b'x'*10000)
    assert len(parser.line)<=parser.LIMIT and len(parser.data)<=parser.LIMIT
    parser.feed(b'\n\ndata: [DONE]\n\n')
    assert parser.status()=='unknown'
    assert p.usage({'prompt_tokens':10,'prompt_cache_hit_tokens':11})['cache_read_tokens'] is None
    assert p.usage({'prompt_tokens':10,'prompt_cache_hit_tokens':2,'prompt_tokens_details':{'cached_tokens':3}})['cache_read_tokens'] is None


@pytest.fixture
def office(store, monkeypatch):
    state={'actor':A,'status':'uploading','calls':0,'http':200}
    async def actor(request, internal):
        if state['actor'] is None:raise IAMFailure(401,'synthetic_logout')
        return state['actor']
    async def upstream(request):
        state['calls']+=1
        if state.get('logout'):state['actor']=None
        return httpx.Response(state['http'],json={'protocol':o.PROTOCOL,'job_id':JOB,'state':state['status'],'outputs':[],
            'private_response':'never retained'})
    monkeypatch.setattr(o,'actor_for',actor)
    monkeypatch.setattr(o,'service_configuration',lambda:('http://synthetic-office:8093','synthetic-private-only'))
    monkeypatch.setattr(o,'_transport',httpx.MockTransport(upstream))
    app=FastAPI();app.include_router(o.router)
    @app.exception_handler(IAMFailure)
    async def failed(request,exc):return JSONResponse({'detail':exc.code},status_code=exc.status)
    return TestClient(app,base_url='https://synthetic-app.invalid'),state


def test_real_office_terminal_only_deduplicated_and_owner_bound(office,store):
    client,state=office
    state['status']='queued'
    assert client.post('/v1/office/jobs/'+JOB+'/run').status_code==200
    assert events(store)==[]
    state['status']='succeeded'
    assert client.get('/v1/office/jobs/'+JOB,headers={'X-Employee-No':B.user_no}).status_code==200
    first=events(store)[0]
    assert first['owner']==A.user_no and first['kind']=='office' and first['status']=='succeeded' and first['duration'] is not None
    for _ in range(3):assert client.get('/v1/office/jobs/'+JOB).status_code==200
    assert events(store)==[first]
    state['actor']=B
    assert client.get('/v1/office/jobs/'+JOB).status_code==200
    second=next(x for x in events(store) if x['owner']==B.user_no)
    assert second['duration'] is None and second['event_id']!=first['event_id']


def test_office_http_failure_or_logout_not_claimed_as_remote_job_failure(office,store):
    client,state=office
    state['http']=503
    assert client.get('/v1/office/jobs/'+JOB).status_code==503
    state.update(http=200,logout=True,status='succeeded')
    assert client.get('/v1/office/jobs/'+JOB).status_code==401
    assert events(store)==[]


def test_office_cancel_receipt_records_cancelled_once(office,store):
    client,state=office;state['status']='cancelled'
    assert client.delete('/v1/office/jobs/'+JOB).status_code==200
    assert events(store)[0]['status']=='cancelled'
    p.shutdown();p.configure(store)
    assert client.get('/v1/office/jobs/'+JOB).status_code==200
    assert len(events(store))==1


def test_office_old_start_is_unknown_without_another_run(store):
    p.office_started(A, JOB, time.monotonic()-3601)
    p.office_observed(A, JOB, 'succeeded')
    assert events(store)[0]['duration'] is None


@pytest.mark.parametrize('dbstate,expected,commit_error',[
    ('completed','succeeded',False),('cancelled','cancelled',False),('failed','failed',False),(None,None,True)
])
def test_actual_project_run_counts_committed_state_only(store,dbstate,expected,commit_error):
    class Cursor:
        def execute(self,sql,args):self.sql=sql
        def fetchone(self):return {'total':0} if 'sum(octet_length' in self.sql else {'status':dbstate}
    class DB:
        def seal(self,*args):return 'synthetic-sealed-result'
        async def tx(self,access,operation):
            result=operation(Cursor(),{'fence':1})
            if commit_error:raise RuntimeError('synthetic commit denied')
            return result
    class Svc:
        async def invoke(self,*args):return {'synthetic':'private result'}
    runtime=object.__new__(project.ProjectRuntime)
    runtime.db=DB();runtime.service=lambda access:(Svc(),None)
    async def auth(access):return None
    runtime.authorize=auth
    access=SimpleNamespace(actor=A,job_id=str(uuid4()),identity=('synthetic-owner','synthetic-session'),owner='synthetic-owner',
        parent_job_id=None,cancelled=asyncio.Event())
    asyncio.run(runtime._run(access,'meeting','status',{},None,time.time()+60))
    rows=events(store)
    if expected:assert len(rows)==1 and rows[0]['status']==expected and rows[0]['kind']=='task'
    else:assert rows==[]


def test_queue_full_does_not_block_request_and_reports_loss():
    started,release=threading.Event(),threading.Event()
    class Blocked:
        def __init__(self):self.losses=[]
        def enable_source(self,k):pass
        def record_event(self,**kw):started.set();release.wait(2)
        def record_loss(self,k,t):self.losses.append(k)
    backend=Blocked();sink=p.Dispatcher(backend,1)
    event={'kind':'model','completed_at':time.time()}
    assert sink.submit(A.user_no,event)
    assert started.wait(1)
    assert sink.submit(A.user_no,event)
    before=time.monotonic()
    assert not sink.submit(A.user_no,event)
    assert time.monotonic()-before<.02
    release.set();assert sink.close()
    assert backend.losses==['model']


def test_writer_errors_not_business_errors_and_loss_visible():
    class Broken:
        def __init__(self):self.losses=[]
        def enable_source(self,k):pass
        def record_event(self,**kw):raise OSError('synthetic disk failed')
        def record_loss(self,k,t):self.losses.append(k)
    backend=Broken();sink=p.Dispatcher(backend)
    assert sink.submit(A.user_no,{'kind':'model','completed_at':time.time()})
    assert sink.close() and backend.losses==['model']


def test_untrusted_actor_shape_cannot_claim_owner():
    assert p.owner(SimpleNamespace(user_no=A.user_no)) is None
    assert p.owner(replace(A,user_no="NOT-AN-EMPLOYEE")) is None
