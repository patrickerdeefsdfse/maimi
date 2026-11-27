import { randomUUID } from 'node:crypto';
import path from 'node:path';

/** The renderer never receives the authorized host path. The shell verifies the digest before native drag. */
export function dragRequest(child,address,signal) {
  if (typeof address!=='string' || !child.connected) return Promise.reject(Error('Native file drag unavailable'));
  const id=randomUUID();
  return new Promise((resolve,reject)=>{
    const stop=(error,value)=>{
      clearTimeout(timer);
      child.off('message',message);
      child.off('exit',exit);
      signal?.removeEventListener('abort',cancel);
      if(error)reject(error);else resolve(value);
    };
    const exit=()=>stop(Error('Native host stopped'));
    const cancel=()=>{
      if(child.connected)child.send({type:'company-file-drag-cancel',id},()=>{});
      stop(Error('Native file drag cancelled'));
    };
    const message=row=>{
      if(row?.type!=='company-file-drag-result'||row.id!==id)return;
      const v=row.value;
      if(row.failed || (v!==null && (!v || !path.isAbsolute(v.path) || !Number.isSafeInteger(v.size) || v.size<0 || !/^[0-9a-f]{64}$/.test(v.sha256))))return stop(Error('Native file drag rejected'));
      stop(undefined,v);
    };
    const timer=setTimeout(cancel,12000);
    child.on('message',message);
    child.once('exit',exit);
    signal?.addEventListener('abort',cancel,{once:true});
    if(signal?.aborted){cancel();return;}
    child.send({type:'company-file-drag',id,address},error=>{if(error)stop(error);});
  });
}

export function serveDrag(getContext,authorize) {
  const active=new Map();
  process.on('message',async row=>{
    if(typeof row?.id!=='string')return;
    if(row.type==='company-file-drag-cancel'){active.get(row.id)?.abort();return;}
    if(row.type!=='company-file-drag'||typeof row.address!=='string'||active.has(row.id))return;
    const abort=new AbortController();
    active.set(row.id,abort);
    try {
      const value=await authorize(getContext(),row.address,abort.signal);
      if(process.connected)process.send({type:'company-file-drag-result',id:row.id,value},()=>{});
    } catch {
      if(process.connected)process.send({type:'company-file-drag-result',id:row.id,failed:true},()=>{});
    } finally {
      active.delete(row.id);
    }
  });
  return ()=>{for(const abort of active.values())abort.abort();};
}
