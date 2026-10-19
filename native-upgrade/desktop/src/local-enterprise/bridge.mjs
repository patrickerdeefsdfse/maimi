import http from 'node:http';
import {randomBytes,randomUUID,createHash,timingSafeEqual} from 'node:crypto';

export const PROTOCOL='megmeet-office-remote-v1';
export const MAX_INPUT=32*1024*1024,MAX_OUTPUT=32*1024*1024;
const JSON_MAX=2*1024*1024,CONTROL_MAX=Math.ceil(MAX_INPUT/3)*4+8192;
const SID=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const JOB=/^[a-f0-9]{32}$/,HEX=/^[a-f0-9]{64}$/;
const CONNECTORS=new Set(['logistics-query','web-search','dingtalk']);
const SELECTABLE=new Set([...CONNECTORS,'service']);
const SERVICE_PREFIX='/v1/connectors/local-service';
const SERVICE_ACTIONS=new Set(['status','list_servers','disconnect','inspect_server','read_file','write_file','run_command']);
const OPERATIONS=new Set(['inspect','read','create','edit','convert','pdf','image','ocr']);
const MUTATING=new Set(['create','edit','convert','pdf','image']);
const RESERVED=new Set(['workspace','limits','protocol','operation','input','output','engineRoot','pythonExecutable','command','env','overwrite','inputs']);
const EXT=/^\.(?:txt|md|csv|xlsx|xls|docx|pptx|pdf|png|jpg|jpeg|webp|gif)$/;
const opaque=()=>randomBytes(32).toString('base64url');
export const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const fail=code=>{throw Object.assign(Error(code),{code});};
const plain=o=>o!==null&&typeof o==='object'&&!Array.isArray(o);
const keys=(o,allowed)=>plain(o)&&Object.keys(o).every(k=>allowed.includes(k));
const same=(a,b)=>typeof a==='string'&&Buffer.byteLength(a)===Buffer.byteLength(b)&&timingSafeEqual(Buffer.from(a),Buffer.from(b));
const active=a=>a&&typeof a.owner==='string'&&a.owner.length>0&&typeof a.token==='string'&&a.token.length>0&&Number.isSafeInteger(a.generation)&&a.expiresAt>Date.now();
const authEqual=(a,b)=>active(a)&&a.owner===b.owner&&a.generation===b.generation&&a.token===b.token;
const filename=s=>typeof s==='string'&&s.length>0&&s.length<=200&&!/[\\/\x00-\x1f\x7f:]/.test(s)&&!/[ .]$/.test(s)&&!/^\./.test(s);
function decode(value,limit=MAX_INPUT){if(typeof value!=='string'||value.length>Math.ceil(limit/3)*4)fail('TRANSFER_TOO_LARGE');const bytes=Buffer.from(value,'base64');if(bytes.length>limit||bytes.toString('base64')!==value)fail('TRANSFER_INVALID');return bytes;}
async function bounded(body,max,guard=()=>{}){const parts=[];let n=0;for await(const part of body??[]){await guard();n+=part.length;if(n>max)fail('TRANSFER_TOO_LARGE');parts.push(Buffer.from(part));}await guard();return Buffer.concat(parts,n);}
function pause(ms,signal){signal.throwIfAborted();return new Promise((resolve,reject)=>{const abort=()=>{clearTimeout(t);reject(Error('TASK_CANCELLED'));};const t=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},ms);signal.addEventListener('abort',abort,{once:true});});}

/** Parent-process credential boundary. Only the reviewed native plugin receives
 * nativeCapability. Model tools receive neither IAM credentials nor this token.
 * Native control calls attest real session and accepted attachment state; they
 * must never be exposed as a model tool or a browser fetch/RPC route. */
export async function createLocalEnterpriseBridge({gatewayOrigin,getAuthorization,testOnly=false,fetchImpl=fetch,pollMs=200}){
 const origin=new URL(gatewayOrigin);
 if(origin.origin!==gatewayOrigin||origin.username||origin.password||!(gatewayOrigin==='https://platform-ai.megmeet.com'||testOnly&&origin.protocol==='http:'&&origin.hostname==='127.0.0.1'&&origin.port))fail('GATEWAY_ORIGIN_DENIED');
 if(typeof getAuthorization!=='function')fail('AUTHORIZATION_REQUIRED');
 const initial=await getAuthorization();if(!active(initial))fail('COMPANY_SIGN_IN_REQUIRED');
 const owner={...initial},nativeCapability=opaque(),sessions=new Map(),byId=new Map(),pending=new Set();
 let closed=false,closing,closingRequested=false,totalStored=0;
 const checkAuth=async()=>{const now=await getAuthorization();if(closed||!authEqual(now,owner))fail('COMPANY_SESSION_CHANGED');return now;};
 const check=async s=>{await checkAuth();if(closingRequested||s.revoked||sessions.get(s.capability)!==s||s.until<=Date.now())fail('LOCAL_SESSION_REVOKED');};
 const revokeService=async s=>{if(!s.service)return;const grant=s.service;s.service=null;try{await remote(s,SERVICE_PREFIX+'/revoke',{method:'POST',body:{},headers:{'x-maimi-service-grant':grant.control_grant},cleanup:true,signal:AbortSignal.timeout(4000)});}catch{/* Lost acknowledgment is never a confirmed remote stop. */}};
 const drop=s=>{s.revoked=true;for(const controller of s.requests)controller.abort();for(const v of s.files.values())totalStored-=v.bytes.length;s.files.clear();sessions.delete(s.capability);if(byId.get(s.id)===s)byId.delete(s.id);return revokeService(s);};
 const prune=()=>{for(const s of sessions.values())if(s.until<=Date.now())drop(s);};
 const remote=async(s,suffix,{method='GET',body,signal,bytes=false,cleanup=false,headers={}}={})=>{
  const auth=cleanup?await checkAuth():await check(s).then(checkAuth);
  const response=await fetchImpl(gatewayOrigin+suffix,{method,headers:{...headers,authorization:'Bearer '+auth.token,...body===undefined?{}:{'content-type':Buffer.isBuffer(body)?'application/octet-stream':'application/json'}},body:body===undefined?undefined:Buffer.isBuffer(body)?body:JSON.stringify(body),signal,redirect:'error'});
  if(!response.ok){await response.body?.cancel();fail('ENTERPRISE_REQUEST_FAILED');}
  const raw=await bounded(response.body,bytes?MAX_OUTPUT:JSON_MAX,cleanup?checkAuth:()=>check(s));
  if(bytes)return raw;
  try{return JSON.parse(raw.toString('utf8'));}catch{fail('ENTERPRISE_RESPONSE_INVALID');}
 };
 async function serviceGrant(s,signal){
  if(!s.selected.includes('service'))fail('SERVICE_NOT_SELECTED');
  if(s.service&&s.service.expires_at*1000>Date.now()+2000)return s.service;
  if(s.serviceOpening){await s.serviceOpening;await check(s);return s.service;}
  s.serviceOpening=(async()=>{
  const grant=await remote(s,SERVICE_PREFIX+'/grants',{method:'POST',body:{native_session_id:s.id},signal});
  if(!/^[A-Za-z0-9_-]{43}$/.test(grant?.control_grant??'')||!/^[A-Za-z0-9_-]{43}$/.test(grant?.tool_grant??'')||!Number.isSafeInteger(grant.expires_at)||grant.expires_at*1000<=Date.now()||grant.expires_at*1000>Date.now()+601000)fail('SERVICE_GRANT_INVALID');
  s.service=grant;return grant;})();
  try{return await s.serviceOpening;}finally{s.serviceOpening=null;}
 }
 const catalog=async(s,signal)=>{const data=await remote(s,'/v1/connectors/catalog',{signal});if(data?.protocol!=='megmeet-connectors-v1'||!Array.isArray(data.items))fail('CONNECTOR_CATALOG_INVALID');return {protocol:data.protocol,items:data.items.filter(item=>CONNECTORS.has(item?.plugin_key)&&s.selected.includes(item.plugin_key))};};
 const validateJob=(value,id)=>{if(value?.protocol!==PROTOCOL||!JOB.test(value.job_id??'')||id&&value.job_id!==id||!['uploading','queued','running','succeeded','failed','cancelled'].includes(value.state)||!Array.isArray(value.outputs)||value.outputs.length>20)fail('OFFICE_RESPONSE_INVALID');return value;};
 const cancelJob=async(s,id)=>{
  try{const v=validateJob(await remote(s,'/v1/office/jobs/'+id,{method:'DELETE',signal:AbortSignal.timeout(3000),cleanup:true}),id);return ['cancelled','succeeded','failed'].includes(v.state)?'ACKNOWLEDGED':'UNKNOWN';}
  catch{return 'UNKNOWN';}
 };
 async function office(s,args,signal){
  if(!s.remoteOffice)fail('REMOTE_OFFICE_NOT_SELECTED');
  if(!keys(args,['operation','input_ids','output_extension','options'])||!OPERATIONS.has(args.operation)||!Array.isArray(args.input_ids)||args.input_ids.length>20||!keys(args.options??{},Object.keys(args.options??{}))||Object.keys(args.options??{}).some(k=>RESERVED.has(k))||JSON.stringify(args.options??{}).length>512*1024)fail('OFFICE_ARGUMENTS_INVALID');
  const mutating=MUTATING.has(args.operation),merge=args.operation==='pdf'&&args.options?.action==='merge';
  if(mutating?!EXT.test(args.output_extension??''):args.output_extension!==undefined)fail('OFFICE_OUTPUT_INVALID');
  if(args.operation==='create'?args.input_ids.length!==0:merge?args.input_ids.length<1:args.input_ids.length!==1)fail('OFFICE_INPUT_INVALID');
  if(new Set(args.input_ids).size!==args.input_ids.length)fail('OFFICE_INPUT_INVALID');
  const inputs=args.input_ids.map((id,i)=>{const entry=s.files.get(id);if(!entry||entry.kind!=='input')fail('FILE_NOT_ACCEPTED_IN_SESSION');const ext=entry.name.slice(entry.name.lastIndexOf('.')).toLowerCase();if(!EXT.test(ext))fail('OFFICE_EXTENSION_INVALID');return {...entry,path:'input-'+i+ext};});
  if(inputs.reduce((n,v)=>n+v.bytes.length,0)>MAX_INPUT)fail('TRANSFER_TOO_LARGE');
  // Existing public Office upload is raw binary and rejects query parameters.
  // Do not claim it decrypts TSD or bypass company upload requirements.
  if(inputs.some(v=>v.bytes.subarray(0,32).toString('ascii').includes('%TSD-Header')))fail('ENCRYPTED_INPUT_REQUIRES_APPROVED_INGESTION');
  const request={operation:args.operation,options:{...args.options},...inputs.length&&!merge?{input:inputs[0].path}:{},...mutating?{output:'output'+args.output_extension}:{}};
  if(merge)request.options.inputs=inputs.map(v=>v.path);
  let job,completed=false;
  try{
   const value=validateJob(await remote(s,'/v1/office/jobs',{method:'POST',body:{protocol:PROTOCOL,request,inputs:inputs.map(v=>({path:v.path,size_bytes:v.bytes.length}))},signal}));
   job=value.job_id;if(value.state!=='uploading')fail('OFFICE_RESPONSE_INVALID');
   for(let i=0;i<inputs.length;i++)validateJob(await remote(s,`/v1/office/jobs/${job}/inputs/${i}`,{method:'PUT',body:inputs[i].bytes,signal}),job);
   validateJob(await remote(s,`/v1/office/jobs/${job}/run`,{method:'POST',body:{},signal}),job);
   let state;for(;;){state=validateJob(await remote(s,'/v1/office/jobs/'+job,{signal}),job);if(state.state==='succeeded')break;if(['failed','cancelled'].includes(state.state))fail('OFFICE_TASK_FAILED');if(!['queued','running'].includes(state.state))fail('OFFICE_RESPONSE_INVALID');await pause(pollMs,signal);}
   if(!Object.hasOwn(state,'result')||state.outputs.length!==(mutating?1:0))fail('OFFICE_RESPONSE_INVALID');
   const files=[];
   for(const output of state.outputs){
    if(!keys(output,['id','path','size_bytes','sha256'])||!/^(?:0|[1-9][0-9]?)$/.test(output.id??'')||output.path!==request.output||!Number.isSafeInteger(output.size_bytes)||output.size_bytes<0||output.size_bytes>MAX_OUTPUT||!HEX.test(output.sha256??''))fail('OFFICE_OUTPUT_INVALID');
    const data=await remote(s,`/v1/office/jobs/${job}/outputs/${output.id}`,{signal,bytes:true});
    if(data.length!==output.size_bytes||sha(data)!==output.sha256)fail('OFFICE_OUTPUT_INTEGRITY');
    await check(s);if(totalStored+data.length>2*MAX_INPUT)fail('FILE_CAPACITY');
    const id=opaque();s.files.set(id,{kind:'output',name:request.output,bytes:data});totalStored+=data.length;files.push({id,name:request.output,bytes:data.length,sha256:output.sha256});
   }
   await check(s);completed=true;return {result:state.result,files};
  }catch(error){if(job){const status=await cancelJob(s,job);if(signal.aborted&&status==='UNKNOWN')fail('REMOTE_CANCELLATION_UNCONFIRMED');}throw error;}
  finally{if(completed&&job)await cancelJob(s,job);}
 }
 const json=(res,status,value)=>{if(res.headersSent){res.destroy();return;}res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify(value));};
 const server=http.createServer(async(req,res)=>{
 let controller,s,watch,watching=false;
  try{
   if(req.headers.origin||req.headers['sec-fetch-site']||req.headers.host!==`127.0.0.1:${server.address().port}`)fail('BROWSER_REQUEST_DENIED');
   await checkAuth();if(closingRequested)fail('LOCAL_BRIDGE_CLOSING');prune();
   const route=req.url;if(!['/native/open','/native/accept-file','/native/revoke','/native/service-connect','/service','/catalog','/invoke','/office','/file'].includes(route))fail('ROUTE_DENIED');
   const native=route.startsWith('/native/');
   if(native){if(!same(req.headers.authorization,'Bearer '+nativeCapability))fail('NATIVE_AUTH_REQUIRED');}
   else{s=sessions.get(req.headers.authorization?.replace(/^Bearer /,''));if(!s)fail('LOCAL_SESSION_REQUIRED');await check(s);}
   if(req.method!=='POST')fail('METHOD_DENIED');
   controller=new AbortController();pending.add(controller);s?.requests.add(controller);
   watch=setInterval(async()=>{if(watching)return;watching=true;try{if(s)await check(s);else await checkAuth();}catch{controller.abort();}finally{watching=false;}},100);watch.unref();
   const timer=setTimeout(()=>controller.abort(),route==='/service'?355000:180000);timer.unref();controller.signal.addEventListener('abort',()=>clearTimeout(timer),{once:true});
   req.once('aborted',()=>controller.abort());res.once('close',()=>controller.abort());
   let body;try{body=JSON.parse((await bounded(req,route==='/native/accept-file'?CONTROL_MAX:JSON_MAX,()=>{controller.signal.throwIfAborted();return checkAuth();})).toString('utf8'));}catch(e){if(e.code)throw e;fail('REQUEST_INVALID');}
   let value;
   if(route==='/native/open'){
    if(!keys(body,['session_id','selected','remote_office'])||!SID.test(body.session_id??'')||!Array.isArray(body.selected)||body.selected.some(x=>!SELECTABLE.has(x))||typeof body.remote_office!=='boolean')fail('NATIVE_SESSION_INVALID');
    if(byId.has(body.session_id))drop(byId.get(body.session_id));if(sessions.size>=32)fail('SESSION_CAPACITY');
    const created={id:body.session_id,capability:opaque(),selected:[...new Set(body.selected)],remoteOffice:body.remote_office,files:new Map(),requests:new Set(),until:Math.min(owner.expiresAt,Date.now()+15*60*1000),revoked:false};
    sessions.set(created.capability,created);byId.set(created.id,created);value={capability:created.capability,expires_at:created.until};
   }else if(route==='/native/revoke'){
    if(!keys(body,['capability']))fail('NATIVE_SESSION_INVALID');const target=sessions.get(body.capability);if(target)await drop(target);value={revoked:true};
   }else if(route==='/native/service-connect'){
    if(!keys(body,['capability','ip','password'])||typeof body.ip!=='string'||body.ip.length>15||typeof body.password!=='string'||body.password.length>4096)fail('SERVICE_FIELDS_INVALID');
    const target=sessions.get(body.capability);if(!target)fail('LOCAL_SESSION_REQUIRED');await check(target);s=target;s.requests.add(controller);
    try{const grant=await serviceGrant(target,controller.signal);value=await remote(target,SERVICE_PREFIX+'/connect',{method:'POST',body:{ip:body.ip,password:body.password},headers:{'x-maimi-service-grant':grant.control_grant},signal:controller.signal});}
    catch(error){await revokeService(target);throw error;}
    finally{body.password='';}
   }else if(route==='/native/accept-file'){
    if(!keys(body,['capability','name','bytes','sha256','data_base64'])||!filename(body.name)||!HEX.test(body.sha256??'')||!Number.isSafeInteger(body.bytes))fail('FILE_PROOF_INVALID');
    const target=sessions.get(body.capability);if(!target)fail('LOCAL_SESSION_REQUIRED');await check(target);
    const bytes=decode(body.data_base64);if(bytes.length!==body.bytes||sha(bytes)!==body.sha256)fail('FILE_PROOF_INVALID');
    if(target.files.size>=20||totalStored+bytes.length>2*MAX_INPUT)fail('FILE_CAPACITY');
    const id=opaque();target.files.set(id,{kind:'input',name:body.name,bytes});totalStored+=bytes.length;value={id,name:body.name,bytes:bytes.length};
   }else if(route==='/catalog'){if(!keys(body,[]))fail('ARGUMENTS_INVALID');value=await catalog(s,controller.signal);}
   else if(route==='/invoke'){
    if(!keys(body,['connector','action','arguments'])||!s.selected.includes(body.connector)||!CONNECTORS.has(body.connector)||typeof body.action!=='string'||!plain(body.arguments))fail('CONNECTOR_NOT_SELECTED');
    const items=(await catalog(s,controller.signal)).items,item=items.find(v=>v.plugin_key===body.connector),tool=item?.tools?.find(v=>v.name===body.action&&v.available===true);if(!tool)fail('CONNECTOR_ACTION_UNAVAILABLE');
    // Existing invoke only prepares writes; human-only confirm is deliberately
    // absent from model routes. Caller must show a real approval UI to confirm.
    value=await remote(s,'/v1/connectors/invoke',{method:'POST',body:{...body,request_id:randomUUID()},signal:controller.signal});
   }else if(route==='/service'){
    if(!keys(body,['action','arguments'])||!SERVICE_ACTIONS.has(body.action)||!plain(body.arguments))fail('SERVICE_ARGUMENTS_INVALID');
    const grant=await serviceGrant(s,controller.signal);value=await remote(s,SERVICE_PREFIX+'/invoke',{method:'POST',body,headers:{'x-maimi-service-grant':grant.tool_grant},signal:controller.signal});
   }else if(route==='/office')value=await office(s,body,controller.signal);
   else if(route==='/file'){
    if(!keys(body,['id']))fail('ARGUMENTS_INVALID');const file=s.files.get(body.id);if(!file||file.kind!=='output')fail('OUTPUT_NOT_OWNED');value={name:file.name,sha256:sha(file.bytes),data_base64:file.bytes.toString('base64')};
   }
   if(s)await check(s);else await checkAuth();json(res,200,value);clearTimeout(timer);
  }catch(error){json(res,400,{error:{code:typeof error.code==='string'&&/^[A-Z_]{1,80}$/.test(error.code)?error.code:'ENTERPRISE_OPERATION_FAILED'}});}
  finally{clearInterval(watch);if(controller){controller.abort();pending.delete(controller);s?.requests.delete(controller);}}
 });
 await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
 return {origin:`http://127.0.0.1:${server.address().port}`,nativeCapability,
  close:()=>closing??=(async()=>{closingRequested=true;for(const c of pending)c.abort();await Promise.allSettled([...sessions.values()].map(drop));closed=true;server.closeAllConnections();await new Promise(resolve=>server.close(resolve));})()};
}
