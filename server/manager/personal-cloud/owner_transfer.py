"""Explicit normal handoff. Existing signed termination is the only stop authority.

Not exposed to employee routes. Caller is a deployment-owned one-shot controller.
An interrupted journal is a durable admission fence, never a timeout-based lease.
"""
import asyncio,hashlib,json,time,uuid
from dataclasses import replace
from psycopg.types.json import Jsonb
from k8s_worker_provider import WorkerSpec
from worker_reservations import require,handle_digest,owner_key
STOP_OBSERVATION_SECONDS=45

async def require_no_transfer(store,op):
 await store.assert_operation(op)
 cur=await op.connection.execute("SELECT transfer_id FROM platform.owner_placement_transfers WHERE owner_key=%s AND phase<>'COMMITTED'",(op.owner,))
 require(await cur.fetchone() is None,'OWNER_PLACEMENT_TRANSFER_PENDING')

async def register_state_binding(store,op,generation,storage,admitted):
 async with op.connection.transaction():
  await store._owner(op);record=await store._reservation(op,generation);node=record['execution_node']
  from state_placement import state_name
  require(storage['state_name']==state_name(op.owner,node),'LOCAL_STATE_BINDING_NAME_CHANGED')
  await op.connection.execute('INSERT INTO platform.owner_state_placements(owner_key,node_name,storage) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(op.owner,node,Jsonb(storage)))
  row=await (await op.connection.execute('SELECT storage FROM platform.owner_state_placements WHERE owner_key=%s AND node_name=%s FOR UPDATE',(op.owner,node))).fetchone()
  require(row['storage']==storage,'NODE_LOCAL_STATE_IDENTITY_CHANGED')

async def event(store,op,transfer,phase,**fields):
 allowed={'portability','release_audit_id','target_storage'};require(set(fields)<=allowed,'TRANSFER_FIELDS_INVALID')
 async with op.connection.transaction():
  await store._owner(op)
  row=await (await op.connection.execute('SELECT * FROM platform.owner_placement_transfers WHERE transfer_id=%s AND owner_key=%s FOR UPDATE',(transfer,op.owner))).fetchone()
  require(row is not None and row['phase']!='COMMITTED','TRANSFER_JOURNAL_CHANGED')
  order={'PREPARING':'DRAINED','DRAINED':'STOPPED','STOPPED':'PVC_BOUND','PVC_BOUND':'COMMITTED'}
  require(order.get(row['phase'])==phase,'TRANSFER_PHASE_CHANGED')
  assignments=['phase=%s','updated_at=clock_timestamp()'];values=[phase]
  for key,value in fields.items():assignments.append(key+'=%s');values.append(Jsonb(value) if key in ('portability','target_storage') else value)
  await op.connection.execute('UPDATE platform.owner_placement_transfers SET '+','.join(assignments)+' WHERE transfer_id=%s',(*values,transfer))
  await store._event(op,row['generation'],'PLACEMENT_'+phase,{'transfer_id':str(transfer),'source_node':row['source_node'],'target_node':row['target_node']})

async def stopped_record(store,op,before):
 current=await store.get(op.owner)
 require(current and current['generation']==before['generation'] and current['handle']==before['handle'] and current['status']=='released' and current.get('release_audit_id'),'TRANSFER_STOP_UNCONFIRMED')
 cur=await op.connection.execute("SELECT details FROM platform.worker_runtime_events WHERE owner_key=%s AND generation=%s AND event_type='RELEASED' ORDER BY event_id DESC LIMIT 1",(op.owner,before['generation']))
 row=await cur.fetchone();require(row and row['details'].get('audit_id')==current['release_audit_id'] and row['details'].get('handle_sha256')==handle_digest(current['handle']) and len(row['details'].get('evidence_sha256',''))==64,'TRANSFER_SIGNED_STOP_AUDIT_MISSING')
 return current

async def transfer_once(manager,owner,target_node,*,expected_generation,expected_handle_sha256,transfer_id):
 """One explicit operation: never starts/replays a new employee task."""
 owner_key(owner);transfer_id=str(uuid.UUID(transfer_id));cfg=manager.provider.config;store=manager.store
 require(cfg.relocatable_chat_state and store.relocatable_chat_state and store.role=='chat' and cfg.workspace_state_backend=='postgres','TRANSFER_FEATURE_NOT_ENABLED')
 cfg.execution_pool.node(target_node)
 async with store.operation(owner) as op,store.shared_files_exclusion(op):
  await require_no_transfer(store,op)
  before=await store.get(owner)
  require(before and before['status']=='ready' and before['purpose']=='worker' and before['generation']==expected_generation and handle_digest(before['handle'])==expected_handle_sha256,'TRANSFER_SOURCE_CHANGED')
  require(target_node!=before['execution_node'],'TRANSFER_TARGET_IS_SOURCE')
  # No pending employee request is moved or re-issued by this controller.
  q=await (await op.connection.execute("SELECT count(*) AS n FROM platform.worker_runtime_queue WHERE owner_key=%s AND state='WAITING'",(owner,))).fetchone()
  require(q['n']==0,'TRANSFER_QUEUE_NOT_EMPTY')
  own=await store._owner(op);source=dict(own['storage'])
  require(own['home_node']==before['execution_node'] and own['storage_admitted'],'TRANSFER_OWNER_BINDING_CHANGED')
  # Readiness verifies current Pod/Service/EndpointSlice and real state/NAS PVs.
  await manager.ready(before)
  from state_placement import state_name
  require(source['state_name']==state_name(owner,own['home_node']),'TRANSFER_LEGACY_STATE_NOT_ADMITTED')
  async with op.connection.transaction():
   await store._owner(op)
   await op.connection.execute("INSERT INTO platform.owner_placement_transfers(transfer_id,owner_key,generation,source_node,target_node,handle_sha256,phase,source_storage) VALUES(%s,%s,%s,%s,%s,%s,'PREPARING',%s)",(transfer_id,owner,before['generation'],before['execution_node'],target_node,expected_handle_sha256,Jsonb(source)))
  # Intent precedes drain/delete; other manager replicas refuse open/admission.
  drained=await manager.control(before,'idle-release')
  require(drained.get('busy') is False and drained.get('draining') is True,'TRANSFER_WORKER_NOT_QUIESCENT')
  portability=await manager.worker_request(before,'/__cloud/state-portability',timeout=10)
  require(portability=={'protocol':'maimi-state-portability/v1','generation':before['generation'],'pod_uid':before['handle']['pod_uid'],'portable':True,'local_preferences_unchanged':True,'legacy_state_files_absent':True},'TRANSFER_LOCAL_STATE_NOT_PORTABLE')
  before['release_pending']=True;await manager.save(op,before)
  await event(store,op,transfer_id,'DRAINED',portability=portability)
  try:await manager.terminate(op,before)
  except Exception:
   # DELETE is sent at most once by the existing UID-bound provider. Only
   # observe and obtain signed stop proof afterwards; no delete retry.
   until=time.monotonic()+STOP_OBSERVATION_SECONDS
   while time.monotonic()<until:
    current=await store.get(owner)
    if current and current['status']=='released':break
    require(current and current['generation']==before['generation'] and current['handle']==before['handle'] and current['status'] in ('deleting','uncertain'),'TRANSFER_STOP_STATE_CHANGED')
    try:
     absent=await manager.provider.observe_deleted(manager.handle(current))
     if absent.get('api_absent'):await manager.finish_termination(op,current);break
    except Exception:pass
    await asyncio.sleep(.25)
  stopped=await stopped_record(store,op,before)
  await event(store,op,transfer_id,'STOPPED',release_audit_id=stopped['release_audit_id'])
  # Target placement is either a newly created PVC or a previously recorded
  # exact UID. Never adopt an unknown preexisting PVC by name after lost reply.
  target_spec=WorkerSpec(owner,own['next_generation'],mode='worker',empty_storage=True,resource_class='chat',node_name=target_node)
  objects=manager.provider.manifests(target_spec);claim=objects['pvcs'][0]
  prior=await (await op.connection.execute('SELECT storage FROM platform.owner_state_placements WHERE owner_key=%s AND node_name=%s',(owner,target_node))).fetchone()
  if prior:
   target=dict(prior['storage']);require(target['files_uid']==source['files_uid'] and target['files_name']==source['files_name'] and target['namespace']==source['namespace'],'TRANSFER_SHARED_FILES_IDENTITY_CHANGED')
   used=await (await op.connection.execute("SELECT count(*) FILTER(WHERE state<>'RELEASED' OR release_audit_id IS NULL) AS unsafe,count(*) FILTER(WHERE native_workspace_id IS NOT NULL) AS admitted FROM platform.worker_runtime_reservations WHERE owner_key=%s AND handle->>'state_pvc_uid'=%s",(owner,target['state_uid']))).fetchone()
   require(used['unsafe']==0 and used['admitted']>0,'TRANSFER_PRIOR_STATE_STOP_UNCONFIRMED');target_admitted=True
  else:
   await store.assert_operation(op)
   status,pvc=await manager.provider.call('POST',manager.provider.base+'/persistentvolumeclaims',claim)
   require(status==201,'TRANSFER_NEW_PVC_CREATE_UNCONFIRMED')
   state_uid=manager.provider.verify(claim,pvc);target={**source,'state_name':claim['metadata']['name'],'state_uid':state_uid};target_admitted=False
  await manager.provider.ensure_owner_storage(target_spec,{'state':target['state_uid'],'files':target['files_uid']})
  await event(store,op,transfer_id,'PVC_BOUND',target_storage=target)
  async with op.connection.transaction():
   fresh=await store._owner(op);await stopped_record(store,op,before);await store.assert_other_role_available(op,allow_workers=False)
   require(fresh['home_node']==before['execution_node'] and fresh['storage']==source and fresh['next_generation']==own['next_generation'],'TRANSFER_COMMIT_SOURCE_CHANGED')
   await op.connection.execute('INSERT INTO platform.owner_state_placements(owner_key,node_name,storage) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(owner,target_node,Jsonb(target)))
   saved=await (await op.connection.execute('SELECT storage FROM platform.owner_state_placements WHERE owner_key=%s AND node_name=%s',(owner,target_node))).fetchone();require(saved['storage']==target,'TRANSFER_TARGET_STORAGE_CHANGED')
   await op.connection.execute('UPDATE platform.worker_runtime_owners SET home_node=%s,storage=%s,storage_admitted=%s WHERE owner_key=%s',(target_node,Jsonb(target),target_admitted,owner))
   await event(store,op,transfer_id,'COMMITTED')
  return {'schema':'maimi-r94-normal-ap-transfer/v1','status':'PLACEMENT_CHANGED_NEXT_OPEN_ALLOWED','transfer_id':transfer_id,'old_generation':before['generation'],'old_release_audit_id':stopped['release_audit_id'],'source_node':before['execution_node'],'target_node':target_node,'files_uid_unchanged':True,'old_state_pvc_retained':True,'new_generation_started':False,'employee_commands_replayed':0}
