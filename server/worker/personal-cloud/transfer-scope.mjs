// The managed attachment projection resolves staged native receipts after admission.
// Only the accepted user event bearing this task's own request ID may contribute files.
import {constants} from 'node:fs';
import {open,opendir} from 'node:fs/promises';
const DIR=constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW;
const fd=(dir,name)=>'/proc/self/fd/'+dir.fd+'/'+name;
async function selectedAliases(root,session,owner,generation,refs){
 const dir=await open(root,DIR).catch(e=>{if(e.code!=='ENOENT')throw e;});
 if(!dir)return;
 try{
  let count=0;
  for await(const item of await opendir('/proc/self/fd/'+dir.fd)){
   if(++count>256)throw Error('ATTACHMENT_CACHE_LIMIT');
   if(!item.isDirectory()||!/^cache-[a-zA-Z0-9]{6}$/.test(item.name))continue;
   let entry,handle;
   try{
    entry=await open(fd(dir,item.name),DIR);
    handle=await open(fd(entry,'maimi-cache-entry.json'),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
    const stat=await handle.stat();if(!stat.isFile()||stat.size>16384)continue;
    const buffer=Buffer.alloc(16385);const {bytesRead}=await handle.read(buffer);if(bytesRead>16384)continue;
    const m=JSON.parse(buffer.toString('utf8',0,bytesRead));
    if(m.schema!=='maimi-r73-attachment-cache/v1'||m.owner!==owner||m.generation!==generation||m.cache_id!==item.name||m.session_id!==session||Date.parse(m.expires_at)<=Date.now())continue;
    for(const row of refs){
     if(row.sha256!==m.sha256||row.bytes!==m.bytes)continue;
     const prefix='attachments/v1/files/'+row.sha256.slice(0,2)+'/'+row.sha256+'/';
     for(const file of m.relative_files||[]){
      if(file.path!==prefix+row.name||file.bytes!==row.bytes||file.sha256!==row.sha256)continue;
      if(row.aliases.length>=4)throw Error('ATTACHMENT_ALIAS_LIMIT');
      row.aliases.push('/workspace/.maimi-cache/'+item.name+'/'+file.path);
     }
    }
   }catch(e){if(e.code!=='ENOENT')throw e;}
   finally{await handle?.close();await entry?.close();}
  }
 }finally{await dir.close();}
}
export function taskTransferScope({project,output,owner,generation,root='/workspace/.maimi-cache'}){
 const pending=new WeakMap();
 return async(task,signal)=>{
  if(task.revoked||!task.inputRequestId)throw Error('FILE_TASK_NOT_ACTIVE');
  if(!pending.has(task)){
   const promise=(async()=>{
    const inputs=[];
    if(task.hasAttachments){
     const value=await project(task.sessionId,signal);
     const rows=value.messages?.filter(v=>v.request_id===task.inputRequestId);
     if(rows?.length!==1||!Array.isArray(rows[0].items)||!rows[0].items.length||rows[0].items.length>32)throw Error('FILE_TASK_INPUT_UNAVAILABLE');
     let total=0;
     for(const row of rows[0].items){
      const r=row.ref,sha256=r?.attachmentId?.slice(7),name=r?.name||'image';
      if(!['file','image'].includes(row.kind)||r.attachmentId!=='sha256:'+sha256||!/^[a-f0-9]{64}$/.test(sha256)||!Number.isSafeInteger(r.bytes)||r.bytes<0||r.bytes>33554432||!name||name==='.'||name==='..'||name.length>255||/[\\/\x00-\x1f\x7f]/.test(name))throw Error('FILE_TASK_INPUT_INVALID');
      total+=r.bytes;if(total>134217728)throw Error('FILE_TASK_INPUT_LIMIT');
      inputs.push({sha256,name,bytes:r.bytes,media:row.kind==='image'?r.mediaType||null:null,aliases:[]});
     }
     await selectedAliases(root,task.sessionId,owner,generation,inputs);
    }
    return inputs;
   })();
   pending.set(task,promise);promise.catch(()=>pending.delete(task));
  }
  return {inputs:await pending.get(task),output_directory:output(task.sessionId)||null};
 };
}
