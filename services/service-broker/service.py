"""Broker lifecycle, durable admission, bounded concurrency and uncertain cancellation."""
from __future__ import annotations
import asyncio
import logging
import time
from journal import Journal
from protocol import Rejected,TERMINAL,canonical,fingerprint,request,require
from ssh_transport import SSHTransport,Execution,Connections

class Broker:
    def __init__(self,registry,state_file,*,transport=None,connection_factory=None,clock=time.time):
        self.registry=registry;self.clock=clock
        self.store=Journal(state_file,clock=clock)
        self.connections=Connections(registry,self.store,connection_factory=connection_factory,clock=clock)
        self.transport=transport or SSHTransport(registry)
        self.tasks={};self.executions={}
        self.capacity=asyncio.Semaphore(2)
        self.closing=False;self.closed=False
    def _audit(self,action,status,error):
        # No command/path/output/employee/session/operation IDs or exception text.
        try:logging.getLogger('maimi.service').info('service_operation action=%s status=%s error=%s',action,status,error or 'NONE')
        except Exception:pass
    async def submit(self,value):
        require(not self.closing,'NOT_CONFIGURED',503)
        value=request(value,self.clock)
        self.registry.allowed(value['employee_no'],value['action'],value['arguments'].get('host_id'))
        scope=value['scope'];session=None
        if value['action']!='list_servers':
            session=await self.connections.get(value['employee_no'],scope['session_id'],scope['generation'])
            # Durable receipts remain readable after disconnect/restart; submit
            # never turns a prior operation into a fresh execution.
            if self.store.by_request(value['request_id']) is None:
                require(session is not None,'CONNECTION_REQUIRED',409)
                require(session.host_id==value['arguments']['host_id'],'HOST_NOT_REGISTERED',403)
                serialized=__import__('json').dumps(value['arguments'])
                require(session.password not in serialized and self.registry.token not in serialized)
        prior=self.store.by_request(value['request_id'])
        require(prior is not None or len(self.tasks)<16,'RATE_LIMIT',429)
        row,created=self.store.insert(value,fingerprint(value,self.registry.generation))
        if created:
            execution=Execution();self.executions[row['operation_id']]=execution
            job=asyncio.create_task(self._work(row['operation_id'],value,execution,session))
            self.tasks[row['operation_id']]=job
            job.add_done_callback(lambda task:self._finished(row['operation_id'],execution,task))
        # This is a durable admission receipt, not the live execution status.
        # An exact duplicate gets the same original acknowledgement and never replays.
        reply=self.store.view(row,initial=True);reply['status']='queued'
        return reply
    def _finished(self,operation,execution,task):
        # A task cancelled before its first instruction never reaches its finally.
        # A callback still finalizes its durable receipt and releases queue capacity.
        try:
            if not task.cancelled():task.exception()
            if not self.closed:
                status='unknown' if execution.dispatched else 'cancelled'
                self.store.state(operation,status,error_code='CANCEL_UNKNOWN' if execution.dispatched else None)
        finally:
            self.executions.pop(operation,None);self.tasks.pop(operation,None)
    async def _work(self,operation,value,execution,session):
        action=value['action'];args=value['arguments'];scope=value['scope']
        timeout=args.get('timeout_seconds',90)
        deadline=min(float(scope['expires_at']),self.clock()+timeout)
        execution.deadline=deadline;execution.scope_expires=scope['expires_at']
        try:
            async with asyncio.timeout(max(0,deadline-self.clock())):
                async with self.capacity:
                    async with session.lock if session is not None else asyncio.Lock():
                        require(self.clock()<scope['expires_at'],'SCOPE_EXPIRED',403)
                        self.registry.allowed(value['employee_no'],action,args.get('host_id'))
                        self.store.state(operation,'running')
                        if action=='list_servers':
                            current=await self.connections.get(value['employee_no'],scope['session_id'],scope['generation'])
                            result={'servers':[current.public()] if current else []}
                        else:
                            current=await self.connections.get(value['employee_no'],scope['session_id'],scope['generation'],touch=True)
                            require(current is session and current.host_id==args['host_id'],'CONNECTION_REQUIRED',409)
                            result=await self.transport.execute(session,action,args,execution)
                            # A disconnected/replaced/revoked session can never publish
                            # remote output after its memory credentials were cleared.
                            current=await self.connections.get(value['employee_no'],scope['session_id'],scope['generation'])
                            require(current is session,'CONNECTION_REQUIRED',409)
                            session.last_used=self.clock()
                        require(self.clock()<scope['expires_at'],'SCOPE_EXPIRED',403)
                        self.store.state(operation,'completed',result)
                        self._audit(action,'completed',None)
        except asyncio.CancelledError:
            execution.close()
            state='unknown' if execution.dispatched else 'cancelled'
            self.store.state(operation,state,error_code='CANCEL_UNKNOWN' if execution.dispatched else None)
            self._audit(action,state,'CANCEL_UNKNOWN' if execution.dispatched else None)
        except TimeoutError:
            execution.close();error='SCOPE_EXPIRED' if self.clock()>=scope['expires_at'] else 'OPERATION_TIMEOUT'
            state='unknown' if execution.dispatched else 'failed';self.store.state(operation,state,error_code=error);self._audit(action,state,error)
        except Rejected as error:
            # Structured file precondition rejection is a known no-write result.
            known_no_effect={'FILE_NOT_FOUND','FILE_TOO_LARGE','FILE_CHANGED','PATH_NOT_ALLOWED','ARGUMENTS_INVALID','PYTHON_UNAVAILABLE'}
            state='unknown' if execution.dispatched and error.code not in known_no_effect else 'failed'
            self.store.state(operation,state,error_code=error.code);self._audit(action,state,error.code)
        except Exception:
            execution.close();state='unknown' if execution.dispatched else 'failed'
            self.store.state(operation,state,error_code='OPERATION_FAILED');self._audit(action,state,'OPERATION_FAILED')
        finally:
            if session is not None:
                row=self.store.by_request(value['request_id'])
                if row and row['status']=='unknown':
                    key=(session.employee,session.session_id)
                    if self.connections.sessions.get(key) is session:await self.connections._drop(key)
            self.executions.pop(operation,None);self.tasks.pop(operation,None)
    def _row(self,operation,employee,job):
        require(canonical(operation) and canonical(job))
        self.registry.allowed(employee,'list_servers')
        return self.store.get(operation,employee,job)
    async def get(self,operation,employee,job):
        value=self.store.view(self._row(operation,employee,job))
        # Expiry must not prevent bound cleanup/status checks, but the broker
        # itself stops delivering tool data even if gateway checks are bypassed.
        if self.clock()>=value['expires_at']:value['result']=None
        return value
    async def cancel(self,operation,employee,job):
        row=self._row(operation,employee,job)
        if row['status'] not in TERMINAL:
            task=self.tasks.get(operation)
            if task:
                self.store.state(operation,'cancelling')
                execution=self.executions.get(operation)
                if execution is not None:execution.cancelled=True
                task.cancel()
                # Response may remain cancelling; gateway must poll for terminal ACK.
                await asyncio.sleep(0)
            else:self.store.state(operation,'unknown',error_code='RESTART_UNKNOWN')
        return await self.get(operation,employee,job)
    async def close(self):
        if self.closed:return
        self.closing=True
        jobs=list(self.tasks.values())
        for job in jobs:job.cancel()
        if jobs:
            try:await asyncio.wait_for(asyncio.gather(*jobs,return_exceptions=True),4)
            except TimeoutError:pass
        for operation,execution in list(self.executions.items()):
            self.store.state(operation,'unknown' if execution.dispatched else 'cancelled',error_code='CANCEL_UNKNOWN' if execution.dispatched else None)
        await self.connections.close()
        self.closed=True;self.store.close()
