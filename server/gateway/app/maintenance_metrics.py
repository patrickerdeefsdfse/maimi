"""Private aggregate metrics; no owner, session, filename or message labels."""
import asyncio,hmac,os
from pathlib import Path
from fastapi import APIRouter,Request
from fastapi.responses import PlainTextResponse
from .history_store import _connect
from .iam_oauth import IAMFailure
router=APIRouter()
PATH='/internal/maintenance/metrics'
def internal_route(request):return request.method=='GET' and request.url.path==PATH
def collect():
    result=[]
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET LOCAL statement_timeout='5s'")
        cur.execute("SELECT status,COALESCE(sum(expected_bytes),0) AS bytes,count(*) AS files FROM platform.storage_files WHERE status<>'deleted' GROUP BY status")
        for row in cur.fetchall():
            if row['status'] not in ('reserved','active','reclaiming'):continue
            result.append('maimi_managed_storage_bytes{state="'+row['status']+'"} '+str(row['bytes']))
        cur.execute("SELECT count(*) AS n,COALESCE(EXTRACT(epoch FROM clock_timestamp()-min(expires_at)),0) AS age FROM platform.storage_files WHERE status<>'deleted' AND expires_at<clock_timestamp()")
        row=cur.fetchone();result+=['maimi_managed_storage_due_files '+str(row['n']),'maimi_managed_storage_oldest_due_seconds '+str(max(0,row['age']))]
        cur.execute("SELECT count(*) AS n FROM platform.native_sessions WHERE purged_at IS NULL AND last_activity_at<clock_timestamp()-interval '14 days' AND (writer_until IS NULL OR writer_until<clock_timestamp())")
        result.append('maimi_native_history_expired_sessions '+str(cur.fetchone()['n']))
        cur.execute('SELECT COALESCE(sum(logical_bytes),0) AS bytes FROM platform.native_history_accounts')
        result.append('maimi_native_history_logical_bytes '+str(cur.fetchone()['bytes']))
        cur.execute("SELECT count(*) FILTER (WHERE state='cleanup_required') AS pending,count(*) FILTER (WHERE state='manual_review') AS manual_review FROM platform.storage_source_copies")
        row=cur.fetchone()
        result.append('maimi_storage_source_copies_pending '+str(row['pending']))
        result.append('maimi_storage_source_copies_manual_review '+str(row['manual_review']))
    return '\n'.join(result)+'\n'
@router.get(PATH)
async def metrics(request:Request):
    try:
        token=Path(os.environ['MAIMI_METRICS_SECRET_FILE']).read_text().strip()
        if not 32<=len(token)<=256:raise ValueError()
    except (KeyError,OSError,ValueError):raise IAMFailure(503,'maintenance_metrics_not_configured') from None
    auth=request.headers.get('authorization','')
    if request.headers.get('origin') or not hmac.compare_digest(auth,'Bearer '+token):raise IAMFailure(403,'maintenance_metrics_denied')
    return PlainTextResponse(await asyncio.to_thread(collect),media_type='text/plain; version=0.0.4',headers={'Cache-Control':'no-store'})
