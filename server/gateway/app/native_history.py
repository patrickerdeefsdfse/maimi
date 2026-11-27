"""Authoritative append-only native history; PostgreSQL commit is the flush barrier.

Every worker transaction fences against the durable runtime generation. A short
renewable *writer* lease handles crashed clients; it never releases runtime ownership.
No file bytes, credentials or business logging are included in responses/logs.
"""
from __future__ import annotations
import asyncio
import json
import re
from uuid import UUID
from functools import partial
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from .history_store import _connect
from .iam_oauth import IAMFailure

router = APIRouter()
INTERNAL = '/internal/native-history'
OPS = frozenset(('create','open','append','read','renew','close','stat','list','admit'))
HEADERS = {'Cache-Control':'no-store','Pragma':'no-cache'}
REAL_ACTIVITY = frozenset(('user/message','assistant/message','tool/call','tool/result','turn/start','turn/end'))

def check(ok, code, status=409):
    if not ok: raise IAMFailure(status, code)

def identity(owner, session=None):
    check(isinstance(owner,str) and re.fullmatch('[a-f0-9]{32}',owner), 'history_identity_invalid',400)
    if session is not None:
        check(isinstance(session,str) and re.fullmatch('[A-Za-z0-9_-]{1,200}',session), 'history_session_invalid',400)

def writer_id(value):
    try: return str(UUID(value))
    except (ValueError,TypeError,AttributeError): raise IAMFailure(400,'history_writer_invalid') from None

def metadata(row):
    return {'header':row['header'],'eventCount':row['event_count'],
            'inheritedEventCount':row['inherited_event_count'],'revision':str(row['revision'])}

class Store:
    def __init__(self, connect=_connect, *, owner_limit=128*1024**2, session_limit=64*1024**2, prompt_headroom=8*1024**2):
        self.connect=connect; self.owner_limit=owner_limit; self.session_limit=session_limit; self.prompt_headroom=prompt_headroom

    def account(self,cur,owner):
        cur.execute('INSERT INTO platform.native_history_accounts(owner_key) VALUES(%s) ON CONFLICT DO NOTHING',(owner,))
        cur.execute('SELECT logical_bytes FROM platform.native_history_accounts WHERE owner_key=%s FOR UPDATE',(owner,))
        return cur.fetchone()['logical_bytes']

    def size(self,cur,value):
        cur.execute('SELECT octet_length(%s::jsonb::text) AS n',(Jsonb(value),))
        return cur.fetchone()['n']

    def budget(self,used,session_bytes,additional):
        check(used+additional<=self.owner_limit,'history_owner_quota_exceeded',413)
        check(session_bytes+additional<=self.session_limit,'history_session_quota_exceeded',413)

    def usage(self,owner):
        identity(owner)
        with self.connect() as conn,conn.cursor() as cur:
            cur.execute('SELECT logical_bytes FROM platform.native_history_accounts WHERE owner_key=%s',(owner,));row=cur.fetchone()
            return {'logical_bytes':row['logical_bytes'] if row else 0,'quota_bytes':self.owner_limit,'session_quota_bytes':self.session_limit,'retention_days':14}

    def fence(self, cur, owner, generation):
        # Locking this row couples commit to manager release. A stale HTTP
        # authorization cannot write after RELEASED/new-generation admission.
        cur.execute("SELECT generation FROM platform.worker_runtime_reservations WHERE owner_key=%s AND generation=%s AND state<>'RELEASED' AND purpose='worker' AND handle IS NOT NULL FOR SHARE",(owner,generation))
        check(cur.fetchone() is not None,'history_generation_lost',403)

    def row(self,cur,owner,session,*,lock=True):
        cur.execute('SELECT *, writer_until>clock_timestamp() AS writer_live FROM platform.native_sessions WHERE owner_key=%s AND session_id=%s'+(' FOR UPDATE' if lock else ''),(owner,session))
        row=cur.fetchone()
        check(row is not None,'history_not_found',404)
        check(row['purged_at'] is None,'history_expired',410)
        return row

    def owned(self,row,writer,generation):
        check(str(row['writer_id'])==writer and row['writer_generation']==generation and row['writer_live'], 'history_ownership_lost')

    def run(self,owner,generation,operation,body):
        identity(owner)
        check(operation in OPS and type(body) is dict,'history_request_invalid',400)
        session=body.get('id')
        if operation!='list':
            check(isinstance(session,str) and bool(session),'history_session_invalid',400)
            identity(owner,session)
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='10s'")
            self.fence(cur,owner,generation)
            used=self.account(cur,owner) if operation in ('create','append','admit') else None
            if operation=='list':
                after=body.get('after','')
                check(isinstance(after,str) and len(after)<=200,'history_request_invalid',400)
                cur.execute('SELECT * FROM platform.native_sessions WHERE owner_key=%s AND purged_at IS NULL AND session_id>%s ORDER BY session_id LIMIT 101',(owner,after))
                rows=cur.fetchall()
                return {'items':[metadata(r) for r in rows[:100]],'next':rows[99]['session_id'] if len(rows)>100 else None}
            if operation=='create':
                header=body.get('header'); inherited=body.get('inheritedEventCount',0)
                check(type(header) is dict and header.get('id')==session and header.get('version')==4,'history_header_invalid',400)
                check(type(inherited) is int and inherited>=0 and (header.get('isSeeded') is True or inherited==0),'history_seed_invalid',400)
                writer=writer_id(body.get('writer'))
                header_bytes=self.size(cur,header)
                cur.execute('''INSERT INTO platform.native_sessions(owner_key,session_id,header,inherited_event_count,writer_id,writer_generation,writer_until,logical_bytes)
                  VALUES(%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '120 seconds',%s) ON CONFLICT DO NOTHING RETURNING *''',
                  (owner,session,Jsonb(header),inherited,writer,generation,header_bytes))
                row=cur.fetchone()
                if row is None:
                    row=self.row(cur,owner,session)
                    # An identical retry after a lost HTTP response is safe.
                    check(str(row['writer_id'])==writer and row['writer_generation']==generation and row['header']==header and row['inherited_event_count']==inherited,'history_already_exists')
                else:
                    self.budget(used,0,header_bytes)
                    cur.execute('UPDATE platform.native_history_accounts SET logical_bytes=logical_bytes+%s WHERE owner_key=%s',(header_bytes,owner))
                return metadata(row)
            row=self.row(cur,owner,session)
            if operation=='admit':
                self.budget(used,row['logical_bytes'],self.prompt_headroom)
                return {'admitted':True,'logical_bytes':used,'session_logical_bytes':row['logical_bytes']}
            if operation=='stat': return metadata(row)
            if operation=='read':
                offset=body.get('offset',0); length=body.get('length',256)
                check(type(offset) is int and offset>=0 and type(length) is int and 0<=length<=256,'history_range_invalid',400)
                cur.execute('SELECT event FROM platform.native_session_events WHERE owner_key=%s AND session_id=%s AND seq>=%s ORDER BY seq LIMIT %s',(owner,session,offset,length))
                return {**metadata(row),'events':[r['event'] for r in cur.fetchall()]}
            writer=writer_id(body.get('writer'))
            if operation=='open':
                check(not row['writer_live'] or row['writer_generation']!=generation or str(row['writer_id'])==writer,'history_already_owned')
                cur.execute("UPDATE platform.native_sessions SET writer_id=%s,writer_generation=%s,writer_until=clock_timestamp()+interval '120 seconds' WHERE owner_key=%s AND session_id=%s",(writer,generation,owner,session))
                return metadata(row)
            if operation=='close' and row['writer_id'] is None: return {'closed':True}
            self.owned(row,writer,generation)
            if operation=='close':
                cur.execute('UPDATE platform.native_sessions SET writer_id=NULL,writer_generation=NULL,writer_until=NULL WHERE owner_key=%s AND session_id=%s',(owner,session))
                return {'closed':True}
            if operation=='append':
                events=body.get('events'); start=body.get('offset')
                check(type(events) is list and len(events)<=256 and type(start) is int and start>=0,'history_batch_invalid',400)
                check(all(type(e) is dict and type(e.get('seq')) is int and e['seq']==start+i and isinstance(e.get('type'),str) for i,e in enumerate(events)),'history_sequence_invalid',400)
                check(start<=row['event_count'],'history_sequence_gap')
                overlap=min(len(events),row['event_count']-start)
                if overlap:
                    cur.execute('SELECT event FROM platform.native_session_events WHERE owner_key=%s AND session_id=%s AND seq>=%s AND seq<%s ORDER BY seq',(owner,session,start,start+overlap))
                    check([r['event'] for r in cur.fetchall()]==events[:overlap],'history_replay_mismatch')
                fresh=events[overlap:]
                added=sum(self.size(cur,e) for e in fresh)
                self.budget(used,row['logical_bytes'],added)
                for event in fresh:
                    cur.execute('INSERT INTO platform.native_session_events VALUES(%s,%s,%s,%s)',(owner,session,event['seq'],Jsonb(event)))
                if fresh:
                    cur.execute('UPDATE platform.native_history_accounts SET logical_bytes=logical_bytes+%s WHERE owner_key=%s',(added,owner))
                    cur.execute('UPDATE platform.native_sessions SET logical_bytes=logical_bytes+%s WHERE owner_key=%s AND session_id=%s',(added,owner,session))
                    activity=any(e['type'] in REAL_ACTIVITY for e in fresh)
                    titles=[e['data']['title'] for e in fresh if e['type']=='session/title' and isinstance(e.get('data'),dict) and isinstance(e['data'].get('title'),str)]
                    cur.execute('''UPDATE platform.native_sessions SET event_count=event_count+%s,revision=revision+1,
                      last_activity_at=CASE WHEN %s THEN clock_timestamp() ELSE last_activity_at END,
                      title=COALESCE(%s,title) WHERE owner_key=%s AND session_id=%s''',(len(fresh),activity,titles[-1][:500] if titles else None,owner,session))
            cur.execute("UPDATE platform.native_sessions SET writer_until=clock_timestamp()+interval '120 seconds' WHERE owner_key=%s AND session_id=%s RETURNING *",(owner,session))
            return metadata(cur.fetchone())

    def history(self,owner,session=None,offset=0):
        identity(owner,session)
        with self.connect() as conn,conn.cursor() as cur:
            if session is None:
                cur.execute('''SELECT session_id,title,event_count,last_activity_at,created_at FROM platform.native_sessions
                  WHERE owner_key=%s AND purged_at IS NULL ORDER BY last_activity_at DESC,session_id LIMIT 101 OFFSET %s''',(owner,offset))
                rows=cur.fetchall()
                return {'items':[{**dict(r),'last_activity_at':r['last_activity_at'].isoformat(),'created_at':r['created_at'].isoformat()} for r in rows[:100]],'next':offset+100 if len(rows)>100 else None,'retention_days':14}
            row=self.row(cur,owner,session,lock=False)
            cur.execute('SELECT event FROM platform.native_session_events WHERE owner_key=%s AND session_id=%s AND seq>=%s ORDER BY seq LIMIT 256',(owner,session,offset))
            events=[r['event'] for r in cur.fetchall()]
            return {**metadata(row),'events':events,'next':offset+len(events) if offset+len(events)<row['event_count'] else None}

    def sweep(self,limit=100):
        with self.connect() as conn,conn.cursor() as cur:
            cur.execute("SELECT to_regclass('platform.storage_files') AS storage_table")
            has_storage=cur.fetchone()['storage_table'] is not None
            cur.execute("SELECT owner_key,session_id FROM platform.native_sessions WHERE purged_at IS NULL AND last_activity_at<clock_timestamp()-interval '14 days' AND (writer_until IS NULL OR writer_until<clock_timestamp()) ORDER BY last_activity_at LIMIT %s",(limit,))
            rows=cur.fetchall()
            purged=0
            for row in rows:
                key=(row['owner_key'],row['session_id'])
                # Same lock order as append/create; two A nodes may sweep safely.
                cur.execute('SELECT owner_key FROM platform.native_history_accounts WHERE owner_key=%s FOR UPDATE SKIP LOCKED',(row['owner_key'],))
                if cur.fetchone() is None:continue
                cur.execute("SELECT logical_bytes FROM platform.native_sessions WHERE owner_key=%s AND session_id=%s AND purged_at IS NULL AND last_activity_at<clock_timestamp()-interval '14 days' AND (writer_until IS NULL OR writer_until<clock_timestamp()) FOR UPDATE SKIP LOCKED",key)
                current=cur.fetchone()
                if current is None:continue
                if has_storage:
                    cur.execute("UPDATE platform.storage_files SET expires_at=LEAST(expires_at,clock_timestamp()),deletion_reason='session_expired' WHERE owner_key=%s AND session_id=%s AND status IN ('reserved','active')",key)
                cur.execute('DELETE FROM platform.native_session_events WHERE owner_key=%s AND session_id=%s',key)
                cur.execute("UPDATE platform.native_sessions SET header=NULL,title='',event_count=0,logical_bytes=0,writer_id=NULL,writer_generation=NULL,writer_until=NULL,purged_at=clock_timestamp(),revision=revision+1 WHERE owner_key=%s AND session_id=%s",key)
                cur.execute('UPDATE platform.native_history_accounts SET logical_bytes=logical_bytes-%s WHERE owner_key=%s',(current['logical_bytes'],row['owner_key']))
                purged+=1
            return {'purged':purged}

    def erase(self,owner,session):
        identity(owner,session)
        with self.connect() as conn,conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout='10s'")
            self.account(cur,owner)
            row=self.row(cur,owner,session)
            check(not row['writer_live'],'history_session_in_use')
            cur.execute("SELECT to_regclass('platform.storage_files') AS t")
            if cur.fetchone()['t'] is not None:
                cur.execute("UPDATE platform.storage_files SET expires_at=LEAST(expires_at,clock_timestamp()),deletion_reason='session_deleted' WHERE owner_key=%s AND session_id=%s AND status IN ('reserved','active')",(owner,session))
            cur.execute('DELETE FROM platform.native_session_events WHERE owner_key=%s AND session_id=%s',(owner,session))
            cur.execute("UPDATE platform.native_sessions SET header=NULL,title='',event_count=0,logical_bytes=0,writer_id=NULL,writer_generation=NULL,writer_until=NULL,purged_at=clock_timestamp(),revision=revision+1 WHERE owner_key=%s AND session_id=%s",(owner,session))
            cur.execute('UPDATE platform.native_history_accounts SET logical_bytes=logical_bytes-%s WHERE owner_key=%s',(row['logical_bytes'],owner))
            return {'deleted':True,'files_queued_for_cleanup':True}

def internal_route(request):
    return request.method=='POST' and request.url.path in {INTERNAL+'/'+op for op in OPS}

@router.post(INTERNAL+'/{operation}')
async def internal(request:Request,operation:str):
    from .personal_cloud import manager
    check(operation in OPS,'history_operation_invalid',404)
    owner=request.headers.get('x-maimi-owner',''); identity(owner)
    token=request.headers.get('authorization','')
    check(re.fullmatch('Bearer [A-Za-z0-9_-]{43}',token),'history_authentication_required',401)
    raw_generation=request.headers.get('x-maimi-generation','')
    check(re.fullmatch('[1-9][0-9]{0,15}',raw_generation),'history_generation_invalid',400)
    generation=int(raw_generation)
    authorized=await manager('POST','/v1/hosts/'+owner+'/history-authorize',{'generation':generation,'route_token':token[7:]})
    check(authorized=={'authorized':True,'generation':generation},'history_authentication_required',403)
    data=bytearray()
    async for chunk in request.stream():
        check(len(data)+len(chunk)<=32*1024**2,'history_batch_too_large',413); data.extend(chunk)
    try: body=json.loads(data)
    except (ValueError,UnicodeError): raise IAMFailure(400,'history_request_invalid') from None
    value=await asyncio.to_thread(Store().run,owner,generation,operation,body)
    return JSONResponse(value,headers=HEADERS)

@router.get('/v1/personal-cloud/history/pg')
async def history(request:Request,session_id:str|None=None,offset:int=0):
    from .personal_cloud import actor_for,owner_key
    actor=await actor_for(request)
    check(0<=offset<=10000000,'history_range_invalid',400)
    value=await asyncio.to_thread(Store().history,owner_key(actor),session_id,offset)
    await actor_for(request)
    return JSONResponse(value,headers=HEADERS)

@router.get('/v1/personal-cloud/history/usage')
async def usage(request:Request):
    from .personal_cloud import actor_for,owner_key
    actor=await actor_for(request)
    return JSONResponse(await asyncio.to_thread(Store().usage,owner_key(actor)),headers=HEADERS)

@router.post('/v1/personal-cloud/history/pg/delete')
async def erase(request:Request,session_id:str):
    from .personal_cloud import actor_for,owner_key
    actor=await actor_for(request)
    return JSONResponse(await asyncio.to_thread(Store().erase,owner_key(actor),session_id),headers=HEADERS)
