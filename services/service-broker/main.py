"""Private gateway-facing HTTP listener; no employee or public login endpoint."""
from __future__ import annotations
from contextlib import asynccontextmanager
import asyncio
import contextlib
import hmac
import os
import logging
import re
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse
from protocol import Rejected,decode,require
from registry import load_registry
from service import Broker

def make_app(broker=None):
    @asynccontextmanager
    async def lifespan(app):
        async def sweep():
            while True:
                await asyncio.sleep(30)
                await broker.connections.expire()
        task=asyncio.create_task(sweep()) if broker is not None else None
        try:yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):await task
            if broker is not None:await broker.close()
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    async def authenticate(request):
        require(broker is not None,'NOT_CONFIGURED',503)
        values=[value for key,value in request.scope['headers'] if key.lower()==b'authorization']
        require(len(values)==1,'NOT_AUTHORIZED',403)
        try:value=values[0].decode('ascii')
        except UnicodeError:raise Rejected('NOT_AUTHORIZED',403) from None
        require(value.startswith('Bearer ') and hmac.compare_digest(value[7:],broker.registry.token),'NOT_AUTHORIZED',403)
    @app.exception_handler(Rejected)
    async def rejected(request,error):return JSONResponse({'error_code':error.code},status_code=error.status,headers={'Cache-Control':'no-store'})
    @app.exception_handler(Exception)
    async def unexpected(request,error):return JSONResponse({'error_code':'OPERATION_FAILED'},status_code=503,headers={'Cache-Control':'no-store'})
    @app.get('/ready')
    async def ready():return JSONResponse({'status':'READY' if broker is not None and not broker.closing else 'NOT_CONFIGURED'},status_code=200 if broker is not None and not broker.closing else 503,headers={'Cache-Control':'no-store'})
    @app.post('/v1/operations')
    async def submit(request:Request):
        await authenticate(request)
        require(request.headers.get('content-type','').split(';',1)[0].strip().lower()=='application/json')
        raw=bytearray()
        async for part in request.stream():
            require(len(raw)+len(part)<=65536);raw.extend(part)
        value=decode(bytes(raw))
        return JSONResponse(await broker.submit(value),status_code=202,headers={'Cache-Control':'no-store'})
    @app.post('/v1/connections')
    async def connect(request:Request):
        await authenticate(request)
        require(request.headers.get('content-type','').split(';',1)[0].strip().lower()=='application/json')
        raw=bytearray()
        try:
            async for part in request.stream():
                require(len(raw)+len(part)<=16384);raw.extend(part)
            value=decode(bytes(raw))
            return JSONResponse(await broker.connections.open(value),headers={'Cache-Control':'no-store'})
        finally:
            raw[:]=b'\x00'*len(raw)
            if 'value' in locals() and isinstance(value,dict) and isinstance(value.get('fields'),dict):value['fields']['password']=''
    async def connection_state(request,delete=False,retire=False):
        await authenticate(request);items=list(request.query_params.multi_items())
        require(len(items)==3 and {key for key,_ in items}=={'employee_no','session_id','generation'})
        query=dict(items);action=broker.connections.retire if retire else broker.connections.disconnect if delete else broker.connections.status
        return JSONResponse(await action(query['employee_no'],query['session_id'],query['generation']),headers={'Cache-Control':'no-store'})
    @app.get('/v1/connections')
    async def connection_status(request:Request):return await connection_state(request)
    @app.delete('/v1/connections')
    async def connection_disconnect(request:Request):return await connection_state(request,True)
    @app.delete('/v1/connection-grants')
    async def connection_revoke(request:Request):return await connection_state(request,retire=True)
    async def scoped(request,operation,delete=False):
        await authenticate(request)
        items=list(request.query_params.multi_items())
        require(len(items)==2 and {key for key,_ in items}=={'employee_no','job_id'})
        query=dict(items)
        action=broker.cancel if delete else broker.get
        return JSONResponse(await action(operation,query['employee_no'],query['job_id']),headers={'Cache-Control':'no-store'})
    @app.get('/v1/operations/{operation}')
    async def poll(request:Request,operation:str):return await scoped(request,operation)
    @app.delete('/v1/operations/{operation}')
    async def cancel(request:Request,operation:str):return await scoped(request,operation,True)
    return app

def configure_process():
    # The reused image uses numeric UID10001 without a passwd entry. AsyncSSH
    # requires a local display name even with an explicit remote SSH username.
    # This is not an authentication identity or a remote environment variable.
    os.environ.setdefault('USER','maimi-service')
    os.umask(0o077)
    # Library debug/transport traces are never a destination for login metadata.
    logging.getLogger('asyncssh').disabled=True
    logging.getLogger('asyncssh').setLevel(logging.CRITICAL)
    logging.getLogger('asyncssh').propagate=False

def configured_app():
    configure_process()
    try:
        registry=load_registry()
        broker=Broker(registry,'/var/lib/maimi-service/operations.sqlite3')
    except Exception:broker=None
    return make_app(broker)

if __name__=='__main__':
    import uvicorn
    uvicorn.run(configured_app(),host='0.0.0.0',port=8090,access_log=False,log_level='warning')
