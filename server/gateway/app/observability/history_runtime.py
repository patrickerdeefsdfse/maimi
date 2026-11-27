"""Gateway-owned monitoring loop and bounded metadata ingress."""
import asyncio, json, os, time
from uuid import uuid4
from fastapi import Request
from fastapi.responses import JSONResponse
from . import history_store as store
from .monitor import HEADERS

def enabled(): return os.environ.get('MAIMI_MONITOR_HISTORY_ENABLED')=='true'

def activity(user,source):
    # Called only after server-side authentication and an interactive request.
    if not enabled():return
    try:
        from app import usage_producer
        if usage_producer._sink is not None:
            usage_producer._sink.submit(user,{'event_id':str(uuid4()),'kind':'activity','source':source,
                'completed_at':time.time(),'status':'succeeded'})
    except Exception: pass # Monitoring cannot turn a successful action into an error.

def install(app,report):
    if not enabled():return
    state={'task':None,'producer':False};stop=asyncio.Event()
    async def poll():
        while not stop.is_set():
            try:
                value=await report()
                await asyncio.to_thread(store.save_sample,value)
                app.state.maimi_history_status='available'
            except Exception:app.state.maimi_history_status='unavailable'
            try:await asyncio.wait_for(stop.wait(),max(5,60-time.time()%60))
            except asyncio.TimeoutError:pass
    @app.on_event('startup')
    async def start():
        from app import usage_producer
        if usage_producer._sink is not None:
            app.state.maimi_history_status='producer_conflict'
            return
        usage_producer.configure(store.EventStore());state['producer']=True
        state['task']=asyncio.create_task(poll(),name='maimi-monitor-history')
    @app.on_event('shutdown')
    async def shutdown():
        stop.set()
        if state['task']:
            state['task'].cancel()
            try:await state['task']
            except asyncio.CancelledError:pass
        if state['producer']:
            from app import usage_producer
            await asyncio.to_thread(usage_producer.shutdown)
    @app.post('/v1/desktop/auth/telemetry')
    async def telemetry(request:Request):
        # Auth middleware resolves this instance from the authenticated grant,
        # never from a submitted owner/device identifier.
        from app.desktop_auth import require_trusted_actor
        if not request.headers.get('authorization','').startswith('Bearer '):
            return JSONResponse({'detail':'desktop_auth_required'},status_code=401,headers=HEADERS)
        actor=await require_trusted_actor(request)
        instance=getattr(request.state,'desktop_instance_id',None)
        if not instance:return JSONResponse({'detail':'device_binding_required'},status_code=403,headers=HEADERS)
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>512:return JSONResponse({'detail':'metadata_too_large'},status_code=413,headers=HEADERS)
        try:
            body=json.loads(data)
            if not isinstance(body,dict) or set(body)!={'version'}:raise ValueError('schema')
            await asyncio.to_thread(store.device_seen,actor.user_no,instance,body['version'])
        except (ValueError,TypeError):return JSONResponse({'detail':'metadata_invalid'},status_code=400,headers=HEADERS)
        except Exception:return JSONResponse({'detail':'monitoring_unavailable'},status_code=503,headers=HEADERS)
        return JSONResponse({'status':'recorded','next_seconds':60},headers=HEADERS)
