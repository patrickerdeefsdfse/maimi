"""Durable administrator assignments. A grant, audit event and item result commit together."""
import asyncio
import csv
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .history_store import _connect
from .enterprise_tasks import CloudRoute

JOB_FIELDS = """id,parent_id,enabled,total,pending,changed,unchanged,failed,created_at,updated_at"""
ITEM_FIELDS = "employee,plugin,version,status,error_code,finished_at"
MAX_ITEMS = 5000
log = logging.getLogger(__name__)


class Payload(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Target(Payload):
    plugin: str = Field(min_length=1,max_length=160)
    version: str = Field(min_length=1,max_length=80)


class Retry(Payload):
    request_id: UUID


class Batch(Retry):
    enabled: StrictBool
    employees: list[str] = Field(min_length=1,max_length=200)
    plugins: list[Target] = Field(min_length=1,max_length=50)


def job_view(row):
    result = {key: row[key] for key in JOB_FIELDS.split(',')}
    if row['pending'] == 0:
        result['status'] = 'completed_with_errors' if row['failed'] else 'completed'
    else:
        result['status'] = 'queued' if row['pending'] == row['total'] else 'running'
    return result


def get_owned(cur, actor, job_id, lock=False):
    sql = 'SELECT '+JOB_FIELDS+',intent_at FROM platform.plugin_batch_jobs WHERE id=%s AND actor=%s'
    cur.execute(sql+(' FOR UPDATE' if lock else ''),(job_id,actor))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404,'batch_not_found')
    return row


def create(actor, body, parent=None):
    from .plugin_admin import USER
    if parent is None:
        employees = sorted(set(body.employees))
        plugins = sorted({(p.plugin,p.version) for p in body.plugins})
        if any(not USER.fullmatch(e) for e in employees) or len(employees)*len(plugins)>MAX_ITEMS:
            raise HTTPException(422,'invalid_assignment')
        enabled = body.enabled
        signature = [enabled,employees,plugins]
    else:
        signature = ['retry',str(parent)]
    fingerprint = hashlib.sha256(json.dumps(signature,separators=(',',':')).encode()).hexdigest()
    with _connect() as conn, conn.cursor() as cur:
        # Serialize duplicate submissions for this admin/key, across both gateways.
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(f'plugin-batch:{actor}:{body.request_id}',))
        cur.execute('SELECT '+JOB_FIELDS+',fingerprint FROM platform.plugin_batch_jobs WHERE actor=%s AND request_id=%s',
                    (actor,body.request_id))
        existing = cur.fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise HTTPException(409,'batch_request_id_reused')
            return {'job':job_view(existing)}
        intent_at = None
        if parent is not None:
            previous = get_owned(cur,actor,parent,lock=True)
            cur.execute('SELECT '+JOB_FIELDS+' FROM platform.plugin_batch_jobs WHERE parent_id=%s AND actor=%s',(parent,actor))
            child = cur.fetchone()
            if child:
                return {'job':job_view(child)}
            if previous['pending']:
                raise HTTPException(409,'batch_not_finished')
            if not previous['failed']:
                raise HTTPException(409,'batch_no_failed_items')
            enabled,total,intent_at = previous['enabled'],previous['failed'],previous['intent_at']
        else:
            total = len(employees)*len(plugins)
        job_id = uuid4()
        cur.execute("""INSERT INTO platform.plugin_batch_jobs
            (id,actor,request_id,fingerprint,parent_id,enabled,total,pending,intent_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,clock_timestamp())) RETURNING """+JOB_FIELDS,
            (job_id,actor,body.request_id,fingerprint,parent,enabled,total,total,intent_at))
        result = job_view(cur.fetchone())
        if parent is not None:
            cur.execute("""INSERT INTO platform.plugin_batch_items(job_id,employee,plugin,version)
                SELECT %s,employee,plugin,version FROM platform.plugin_batch_items
                WHERE job_id=%s AND status='failed'""",(job_id,parent))
        else:
            keys,versions = zip(*plugins)
            cur.execute("""INSERT INTO platform.plugin_batch_items(job_id,employee,plugin,version)
                SELECT %s,e,p.plugin,p.version FROM unnest(%s::text[]) AS e
                CROSS JOIN unnest(%s::text[],%s::text[]) AS p(plugin,version)""",
                (job_id,employees,list(keys),list(versions)))
        return {'job':result}


def history(actor,offset,limit):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute('SELECT count(*) AS n FROM platform.plugin_batch_jobs WHERE actor=%s',(actor,))
        total = cur.fetchone()['n']
        cur.execute('SELECT '+JOB_FIELDS+' FROM platform.plugin_batch_jobs WHERE actor=%s ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s',
                    (actor,limit,offset))
        return {'jobs':[job_view(r) for r in cur.fetchall()],'total':total}


def detail(actor,job_id,offset,limit):
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        job = get_owned(cur,actor,job_id)
        cur.execute('SELECT '+ITEM_FIELDS+' FROM platform.plugin_batch_items WHERE job_id=%s ORDER BY employee,plugin,version LIMIT %s OFFSET %s',
                    (job_id,limit,offset))
        return {'job':job_view(job),'items':cur.fetchall(),'total':job['total']}


def run_one(conn):
    from .plugin_admin import Change, change_in_transaction, lock_role, role_in_transaction
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("""SELECT id,actor,enabled,intent_at FROM platform.plugin_batch_jobs
            WHERE pending>0 ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""")
        job = cur.fetchone()
        if not job:
            return False
        cur.execute("""SELECT employee,plugin,version FROM platform.plugin_batch_items
            WHERE job_id=%s AND status='pending' ORDER BY employee,plugin,version LIMIT 1 FOR UPDATE""",(job['id'],))
        item = cur.fetchone()
        if not item:
            raise RuntimeError('plugin_batch_item_missing')
        error = None
        try:
            with conn.transaction():
                lock_role(cur, job['actor'], shared=True)
                if role_in_transaction(cur,job['actor'])['role'] == 'reader':
                    raise HTTPException(403,'plugin_admin_required')
                body = Change(**item,enabled=job['enabled'])
                changed = change_in_transaction(cur,body,job['actor'],job['intent_at'])
                status = 'changed' if changed else 'unchanged'
        except HTTPException as exc:
            status,error = 'failed',exc.detail
        except (psycopg.DataError,psycopg.IntegrityError):
            status,error = 'failed','batch_item_failed'
            log.warning('Plugin batch item failed database validation for job %s',job['id'])
        cur.execute("""UPDATE platform.plugin_batch_items SET status=%s,error_code=%s,finished_at=clock_timestamp()
            WHERE job_id=%s AND employee=%s AND plugin=%s AND version=%s""",
            (status,error,job['id'],item['employee'],item['plugin'],item['version']))
        cur.execute("""UPDATE platform.plugin_batch_jobs SET pending=pending-1,changed=changed+%s,
            unchanged=unchanged+%s,failed=failed+%s,updated_at=clock_timestamp() WHERE id=%s""",
            (int(status=='changed'),int(status=='unchanged'),int(status=='failed'),job['id']))
        return True


def worker_connection():
    conn = _connect()
    try:
        with conn.transaction():
            conn.execute("SET statement_timeout TO '10s'")
            conn.execute("SET lock_timeout TO '4s'")
        return conn
    except Exception:
        conn.close()
        raise


def process_chunk(conn):
    for _ in range(16):
        if not run_one(conn):
            return False
    return True


def run_chunk():
    with worker_connection() as conn:
        return process_chunk(conn)


async def work(stop):
    delay = 2
    conn = None
    try:
        while not stop.is_set():
            try:
                if conn is None:
                    conn = await asyncio.to_thread(worker_connection)
                busy = await asyncio.to_thread(process_chunk,conn)
                delay = 0 if busy else 3
            except Exception as exc:
                # Closing the connection rolls back its unfinished transaction.
                if conn is not None:
                    await asyncio.to_thread(conn.close)
                    conn = None
                log.warning('Plugin batch worker will retry (%s)',type(exc).__name__)
                delay = min(max(3,delay*2),30)
            if delay:
                try:
                    await asyncio.wait_for(stop.wait(),delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        if conn is not None:
            await asyncio.to_thread(conn.close)


@asynccontextmanager
async def lifespan(app):
    stop = asyncio.Event()
    worker = asyncio.create_task(work(stop))
    try:
        yield
    finally:
        stop.set()
        await worker


router = APIRouter(prefix='/v1/plugin-admin/batches',lifespan=lifespan,route_class=CloudRoute)


def configured_admin(request):
    from .plugin_admin import configured_admin as check
    return check(request)


@router.post('',status_code=202)
def submit(body:Batch,request:Request):
    return create(configured_admin(request),body)


@router.get('')
def list_jobs(request:Request,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=100)):
    return history(configured_admin(request),offset,limit)


@router.get('/{job_id}')
def get_job(job_id:UUID,request:Request,offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200)):
    return detail(configured_admin(request),job_id,offset,limit)


@router.post('/{job_id}/retry',status_code=202)
def retry(job_id:UUID,body:Retry,request:Request):
    return create(configured_admin(request),body,job_id)


def csv_cell(value):
    text = str(value)
    if text.lstrip().startswith(('=','+','-','@')) or text[:1] in ('\t','\n','\r'):
        return "'"+text
    return text


@router.get('/{job_id}/export.csv')
def export(job_id:UUID,request:Request):
    report = detail(configured_admin(request),job_id,0,MAX_ITEMS)
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(('任务编号','工号','插件','版本','操作','结果','失败原因','完成时间'))
    labels = {'pending':'待处理','changed':'已变更','unchanged':'已是目标状态','failed':'失败'}
    reasons = {'plugin_release_not_found':'版本未发布或已停用','plugin_assignment_changed':'已有更新的授权决定',
               'plugin_admin_required':'发起人管理权限已变更','invalid_assignment':'工号或版本无效',
               'batch_item_failed':'该项未能保存'}
    for item in report['items']:
        row = (job_id,item['employee'],item['plugin'],item['version'],'开通' if report['job']['enabled'] else '收回',
               labels[item['status']],reasons.get(item['error_code'],item['error_code'] or ''),
               item['finished_at'].isoformat() if item['finished_at'] else '')
        writer.writerow([csv_cell(v) for v in row])
    return Response(stream.getvalue().encode('utf-8-sig'),media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="maimi-plugin-batch-{job_id}.csv"',
                             'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
