// Private fixed-helper bridge. Root/owner come from worker bootstrap, never HTTP input.
import {spawn} from 'node:child_process';
import {once} from 'node:events';
const fail=(code,status=503)=>{throw Object.assign(Error(code),{code,status});};
const plain=x=>x&&typeof x==='object'&&!Array.isArray(x);
const exact=(value,fields)=>plain(value)&&Object.keys(value).sort().join(',')===[...fields].sort().join(',');
const SID=/^cfs_[a-f0-9]{32}$/, SHA=/^[a-f0-9]{64}$/;
const MAX=128*1024*1024;
export function validateExpiryRequest(body){
 if(!exact(body,['protocol','refs'])||body.protocol!=='card-retention-v1'||!Array.isArray(body.refs)||body.refs.length<1||body.refs.length>64)fail('CARD_EXPIRE_INVALID',400);
 const seen=new Set();
 for(const ref of body.refs){
  if(!exact(ref,['storage_id','sha256','bytes','expires_at'])||!SID.test(ref.storage_id)||!SHA.test(ref.sha256)||!Number.isSafeInteger(ref.bytes)||ref.bytes<1||ref.bytes>MAX||!Number.isSafeInteger(ref.expires_at)||ref.expires_at<172800||seen.has(ref.storage_id))fail('CARD_EXPIRE_INVALID',400);
  seen.add(ref.storage_id);
 }
 return {refs:body.refs};
}
function helperError(value){
 if(value?.code==='BUSY')fail('PROJECT_CARD_STORE_BUSY',429);
 if(value?.code==='EXPIRED')fail('project_card_file_expired',410);
 if(value?.code==='NOT_FOUND')fail('project_card_file_missing',404);
 fail('PROJECT_CARD_STORE_REJECTED',503);
}
export function createCardStoreClient({root='/state/dsh',owner,helper='/opt/personal-cloud/card_store.py',python='python3',spawnProcess=spawn}={}){
 function launch(operation,request,body,signal){
  if(!/^[a-f0-9]{32}$/.test(owner||'')||!['save','read','expire'].includes(operation))fail('PROJECT_CARD_OWNER_REQUIRED');
  const header=Buffer.from(JSON.stringify(request)+'\n');if(header.length>32768)fail('PROJECT_CARD_METADATA_TOO_LARGE',413);
  signal?.throwIfAborted();
  const child=spawnProcess(python,['-I','-B',helper,'--root',root,'--owner',owner,'--operation',operation],
   {env:{PATH:'/usr/local/bin:/usr/bin:/bin',LANG:'C.UTF-8'},stdio:['pipe','pipe','pipe']});
  let stderrBytes=0,stopReason=null;
  const stop=()=>{stopReason='PROJECT_CARD_STORE_INTERRUPTED';child.kill('SIGKILL');};
  const timer=setTimeout(stop,65000);timer.unref?.();signal?.addEventListener('abort',stop,{once:true});
  child.stderr.on('data',chunk=>{stderrBytes+=chunk.length;if(stderrBytes>8192)stop();});
  child.stdin.on('error',()=>{});
  const exit=new Promise((resolve,reject)=>{
   child.once('error',()=>reject(Object.assign(Error('PROJECT_CARD_STORE_UNAVAILABLE'),{code:'PROJECT_CARD_STORE_UNAVAILABLE',status:503})));
   child.once('close',code=>{clearTimeout(timer);signal?.removeEventListener('abort',stop);resolve({code,stopReason});});
  });
  exit.catch(()=>{});
  const input=(async()=>{
   const write=async chunk=>{signal?.throwIfAborted();if(!child.stdin.write(chunk))await once(child.stdin,'drain',{signal});};
   try{await write(header);if(body)for await(const chunk of body)await write(chunk);child.stdin.end();}
   catch(error){stop();throw error;}
  })();input.catch(()=>{});
  return {child,exit,input,stop};
 }
 async function small(operation,request,body,signal){
  const task=launch(operation,request,body,signal);let size=0;const chunks=[];
  try{
   for await(const chunk of task.child.stdout){size+=chunk.length;if(size>32768)fail('PROJECT_CARD_STORE_RESPONSE_LIMIT');chunks.push(chunk);}
   const finished=await task.exit;await task.input;signal?.throwIfAborted();
   let value;try{value=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{fail('PROJECT_CARD_STORE_RESPONSE_INVALID');}
   if(value?.status==='ERROR')helperError(value);
   if(finished.stopReason||![0,2].includes(finished.code))fail('PROJECT_CARD_STORE_FAILED');
   if(operation==='save'&&(finished.code!==0||value?.status!=='SAVED'))fail('PROJECT_CARD_STORE_FAILED');
   if(operation==='expire'&&!['COMPLETE','PARTIAL'].includes(value?.status))fail('PROJECT_CARD_STORE_FAILED');
   return value;
  }finally{task.stop();}
 }
 return {
  async save(descriptor,body,signal){
   if(!plain(descriptor)||descriptor.owner_key!==owner||descriptor.storage_kind!=='card-v1')fail('PROJECT_CARD_OWNER_MISMATCH',403);
   return small('save',descriptor,body,signal);
  },
  async expire(body,signal){return small('expire',validateExpiryRequest(body),null,signal);},
  async read(request,signal){
   if(!exact(request,['storage_id','artifact_id','maximum','expected_bytes'])||!SID.test(request.storage_id)||!/^pjf_[A-Za-z0-9_-]{32}$/.test(request.artifact_id)||!Number.isSafeInteger(request.maximum)||request.maximum<1||request.maximum>MAX||!Number.isSafeInteger(request.expected_bytes)||request.expected_bytes<1||request.expected_bytes>request.maximum)fail('PROJECT_CARD_READ_INVALID',400);
   const task=launch('read',{storage_id:request.storage_id,artifact_id:request.artifact_id},null,signal);
   const iterator=task.child.stdout[Symbol.asyncIterator]();let pending=Buffer.alloc(0),header;
   try{
    for(;;){const next=await iterator.next();if(next.done)fail('PROJECT_CARD_STORE_RESPONSE_INVALID');pending=Buffer.concat([pending,next.value]);const split=pending.indexOf(10);
     if(split>=0){if(split>32768)fail('PROJECT_CARD_STORE_RESPONSE_LIMIT');try{header=JSON.parse(pending.subarray(0,split).toString('utf8'));}catch{fail('PROJECT_CARD_STORE_RESPONSE_INVALID');}pending=pending.subarray(split+1);break;}
     if(pending.length>32768)fail('PROJECT_CARD_STORE_RESPONSE_LIMIT');}
    if(header?.status==='ERROR')helperError(header);
    if(header?.status!=='READY'||header.storage_kind!=='card-v1'||header.storage_id!==request.storage_id||header.ref?.bytes!==request.expected_bytes||!/^sha256:[a-f0-9]{64}$/.test(header.ref?.attachmentId||''))fail('PROJECT_CARD_STORE_RESPONSE_INVALID');
    async function* stream(){
     let total=0,last=null;
     try{
      for(;;){signal?.throwIfAborted();if(pending.length){total+=pending.length;if(total>request.expected_bytes)fail('PROJECT_CARD_STORE_LENGTH');if(last)yield last;last=pending;}
       const next=await iterator.next();if(next.done)break;pending=next.value;}
      const finished=await task.exit;await task.input;signal?.throwIfAborted();
      if(finished.code!==0||finished.stopReason||total!==request.expected_bytes)fail('PROJECT_CARD_STORE_FAILED');
      if(last)yield last;
     }finally{task.stop();}
    }
    return {header,stream:stream()};
   }catch(error){task.stop();throw error;}
  }
 };
}
