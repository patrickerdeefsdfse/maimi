import fs from 'node:fs/promises';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {MAX_ZIP, decodePackage, releaseRecord} from './distributed-packages.mjs';

const COMPANY='https://desktop-gateway.example.invalid';
const POLL=60_000;
const GRACE=180_000;
const tag=row=>row.plugin_key+'/'+row.version+'/'+row.sha256;
const deviceIdPattern=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

async function readBounded(response,limit) {
  if(Number(response.headers.get('content-length'))>limit){await response.body?.cancel();throw Error('DISTRIBUTION_RESPONSE_LIMIT');}
  const chunks=[];let size=0;
  for await (const chunk of response.body??[]) {
    size+=chunk.byteLength;
    if(size>limit)throw Error('DISTRIBUTION_RESPONSE_LIMIT');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks,size);
}

// Runtime registrations belong to this signed-in account and vanish with its
// host. Only receipt metadata persists between starts, never the bearer or code.
export function createDistributionSync({home,runtimeRoot,gatewayOrigin=COMPANY,testOnly=false,getAuthorization,apply,
  fetchImpl=globalThis.fetch,now=Date.now,nativeDigests=[]}) {
  const url=new URL(gatewayOrigin);
  if(url.origin!==gatewayOrigin || (url.origin!==COMPANY && !(testOnly && url.protocol==='http:' && url.hostname==='127.0.0.1')))throw Error('DISTRIBUTION_ORIGIN_INVALID');
  const trusted=new Set(nativeDigests);
  const live=new Map(),known=new Map(),pending=new Map(),failures=new Set();
  const ledger=path.join(home,'company-plugin-receipts.v1.json');
  let deviceId=randomUUID();
  let closed=false,loading,active,controller,timer,lastVerified=0,nextKey;

  async function load() {
    let info;
    try{info=await fs.lstat(ledger);}catch(e){if(e.code==='ENOENT')return;throw e;}
    if(!info.isFile() || info.isSymbolicLink() || info.size>2_000_000)throw Error('DISTRIBUTION_LEDGER_INVALID');
    const saved=JSON.parse(await fs.readFile(ledger,'utf8'));
    if(saved.schema!==1 || !Array.isArray(saved.known)||!Array.isArray(saved.pending))throw Error('DISTRIBUTION_LEDGER_INVALID');
    if(saved.deviceId!==undefined){
      if(!deviceIdPattern.test(saved.deviceId))throw Error('DISTRIBUTION_LEDGER_INVALID');
      deviceId=saved.deviceId;
    }
    const rows=saved.known.map(releaseRecord);
    const events=saved.pending.map(e=>{
      if(!['installed','removed','failed'].includes(e.status))throw Error('DISTRIBUTION_LEDGER_INVALID');
      return {row:releaseRecord(e.row),status:e.status};
    });
    for(const row of rows)known.set(tag(row),row);
    for(const e of events)pending.set(tag(e.row),e);
  }
  async function persist() {
    const temp=ledger+'.'+randomUUID()+'.tmp';
    try{
      await fs.writeFile(temp,JSON.stringify({schema:1,deviceId,known:[...known.values()],pending:[...pending.values()]}),{flag:'wx',mode:0o600});
      await fs.rename(temp,ledger);
    }finally{await fs.rm(temp,{force:true});}
  }
  async function request(endpoint,token,body,limit) {
    const signal=AbortSignal.any([controller.signal,AbortSignal.timeout(15_000)]);
    const response=await fetchImpl(url.origin+endpoint,{method:body?'POST':'GET',redirect:'error',signal,
      headers:{Authorization:'Bearer '+token,...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});
    if(!response.ok){
      const error=Error('DISTRIBUTION_HTTP_FAILED');
      error.status=response.status;
      if(body && (response.status===403 || response.status===404)){
        // The gateway distinguishes a retired item from an account-wide refusal.
        try{
          const detail=JSON.parse((await readBounded(response,4096)).toString('utf8')).detail;
          if(detail==='plugin_receipt_scope' || detail==='plugin_release_unavailable')error.detail=detail;
        }catch{/* Keep the original HTTP status when a proxy supplies another body. */}
      }else await response.body?.cancel();
      throw error;
    }
    return readBounded(response,limit);
  }
  async function mark(row,status) {
    const id=tag(row);
    if(status==='failed' && failures.has(id))return;
    if(status==='removed')known.delete(id);
    else known.set(id,row);
    pending.set(id,{row,status});
    await persist();
    if(status==='failed')failures.add(id);else failures.delete(id);
  }
  async function clearRuntime() {
    for(const row of [...live.values()]){
      await apply({action:'remove',row});
      live.delete(row.plugin_key);
    }
  }
  async function run() {
    loading??=load().catch(error=>{loading=undefined;throw error;});
    await loading;
    const auth=getAuthorization();
    if(!auth?.token || !(auth.expiresAt>now())){await clearRuntime();return;}
    let desired;
    try{
      const body=JSON.parse((await request('/v1/plugins/distribution?client=desktop',auth.token,undefined,512*1024)).toString('utf8'));
      if(!Array.isArray(body.items)||body.items.length>256)throw Error('DISTRIBUTION_CATALOG_INVALID');
      const rows=body.items.map(releaseRecord);
      desired=new Map(rows.map(row=>[row.plugin_key,row]));
      if(desired.size!==rows.length)throw Error('DISTRIBUTION_CATALOG_INVALID');
      lastVerified=now();
    }catch(e){
      if(e.status===401||e.status===403 || now()-lastVerified>=GRACE)await clearRuntime();
      throw e;
    }
    for(const row of [...known.values()]){
      if(desired.get(row.plugin_key)?.sha256===row.sha256)continue;
      await apply({action:'remove',row});
      if(live.get(row.plugin_key)?.sha256===row.sha256)live.delete(row.plugin_key);
      await mark(row,'removed');
    }
    const rows=[...desired.values()];
    const start=Math.max(0,rows.findIndex(row=>row.plugin_key===nextKey));
    for(const row of [...rows.slice(start),...rows.slice(0,start)]){
      if(closed)break;
      nextKey=row.plugin_key;
      if(now()-lastVerified>=POLL/2)break;
      if(live.get(row.plugin_key)?.sha256===row.sha256)continue;
      let started=false;
      try {
        if(row.kind==='native'&&!trusted.has(row.sha256))throw Error('DISTRIBUTION_NATIVE_NOT_APPROVED');
        const bytes=await request(row.package_url,auth.token,undefined,MAX_ZIP);
        const decoded=await decodePackage(row,bytes,runtimeRoot);
        if(closed || auth.expiresAt<=now())break;
        started=true;
        await apply({action:'install',...decoded});
        live.set(row.plugin_key,row);
        await mark(row,'installed');
      }catch(e){
        if(closed)break;
        if(started)await apply({action:'remove',row});
        await mark(row,'failed');
      }
    }
    let changed=false;
    for(const [id,event] of [...pending]){
      if(closed)break;
      try{
        const {row,status}=event;
        const report={client:'desktop',device_id:deviceId,plugin_key:row.plugin_key,version:row.version,sha256:row.sha256,status};
        if(status!=='installed')report.usable=false;
        else if(live.get(row.plugin_key)?.sha256===row.sha256)report.usable=true;
        const endpoint='/v1/plugins/distribution/receipt';
        try{
          await request(endpoint,auth.token,report,16*1024);
        }catch(error){
          if(error.status!==422)throw error;
          // Gateways predating device reports strictly reject extra fields.
          // The base receipt is idempotent and still enforces the same grant.
          const {device_id,usable,...baseReport}=report;
          await request(endpoint,auth.token,baseReport,16*1024);
        }
        pending.delete(id);changed=true;
      }catch(e){
        const itemScope=e.status===403 && e.detail==='plugin_receipt_scope';
        const retired=(e.status===404 && e.detail==='plugin_release_unavailable') || e.status===410;
        if(itemScope || retired){
          // This refresh has a fresh inventory. A removal receipt for a version
          // no longer present has no recipient when that grant or release is gone.
          if(event.status==='removed' && desired.get(event.row.plugin_key)?.sha256!==event.row.sha256){
            pending.delete(id);changed=true;
          }
          continue;
        }
        if(e.status===404 || e.status===409)continue;
        break;
      }
    }
    if(changed)await persist();
  }
  function refresh(){
    if(closed)return Promise.resolve();
    if(active)return active;
    controller=new AbortController();
    active=run().finally(()=>{active=undefined;controller=undefined;});
    return active;
  }
  async function tick(){
    try{await refresh();}catch{/* Retry when the gateway or native host is ready. */}
    if(!closed){timer=setTimeout(tick,POLL);timer.unref?.();}
  }
  return {start(){if(!closed&&!timer){timer=setTimeout(tick,0);timer.unref?.();}},refresh,
    stop(){closed=true;clearTimeout(timer);controller?.abort();}};
}
