/** Owner-scoped immutable raw history generation before rc20's first write.
 * Called only inside the existing exclusive offline history fence. No account,
 * model credentials, attachments, workspace files or network requests are used.
 */
import {mkdir,open,link,unlink,lstat,readdir} from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {migrationInternals as I} from './history-native.mjs';
export const SNAPSHOT_ROOT='.history-runtime-020-snapshot';
const RECEIPT_FILE='prior-r29-receipt.json',RECEIPT_STAGE='.prior-r29-receipt.partial';
const formats=/^session\.v[34]\.jsonl(?:\.zstd)?$/;
const segment=x=>typeof x==='string'&&x.length>0&&!['.','..'].includes(x)&&!/[\\/\x00-\x1f:]/.test(x);
const validEntry=e=>e&&typeof e.sessionId==='string'&&/^[A-Za-z0-9_-]{1,200}$/.test(e.sessionId)&&Array.isArray(e.relative)&&e.relative.length===2&&e.relative.every(segment)&&Array.isArray(e.files)&&e.files.length>0&&e.files.length<=2&&e.files.every(f=>f&&formats.test(f.name)&&/^[a-f0-9]{64}$/.test(f.sha256)&&Number.isSafeInteger(f.bytes)&&f.bytes>=0);
export async function preserveRuntimeGeneration(options){
 try{return await preserve(options);}catch(error){
  if(["ENOSPC","EDQUOT","EACCES","EROFS","EIO"].includes(error.code))I.fail("RUNTIME_SNAPSHOT_STORAGE_UNAVAILABLE");
  throw error;
 }
}
async function preserve({stateRoot,context,items,deleted,maxBytes,onCheckpoint=async()=>{}}){
 const root=path.join(stateRoot,SNAPSHOT_ROOT),backups=path.join(root,'backups');
 await context.fence();
 let record=null;
 if(await I.safeDirectory(root,true)){
  const names=await readdir(root);
  if(names.some(x=>!['manifest.json','backups',RECEIPT_FILE,RECEIPT_STAGE].includes(x)&&!/^\.manifest-[a-f0-9-]{36}$/.test(x)))I.fail('RUNTIME_SNAPSHOT_INVALID');
  if(await I.exists(path.join(root,'manifest.json'))){
   try{record=JSON.parse((await I.rawFile(path.join(root,'manifest.json'),8*1024*1024)).bytes);}catch(e){if(e.code)throw e;I.fail('RUNTIME_SNAPSHOT_INVALID');}
   if(record?.schema!=='owned-runtime-020-snapshot-v1'||record.runtime!=='0.2.0-rc.2'||!['preparing','complete'].includes(record.status)||!Array.isArray(record.items)||record.items.length>2000||record.items.some(e=>!validEntry(e))||new Set(record.items.map(e=>e.sessionId)).size!==record.items.length)I.fail('RUNTIME_SNAPSHOT_INVALID');
   if(record.receipt!==undefined&&(!record.receipt||!/^[a-f0-9]{64}$/.test(record.receipt.sha256)||!Number.isSafeInteger(record.receipt.bytes)||record.receipt.bytes<0||record.receipt.bytes>8*1024*1024))I.fail('RUNTIME_SNAPSHOT_INVALID');
  }
 }
 if(!record){
  if(!await I.safeDirectory(root,true)){await mkdir(root,{mode:0o700});await I.syncDirectory(stateRoot);}
  if(!await I.safeDirectory(backups,true)){await mkdir(backups,{mode:0o700});await I.syncDirectory(root);}
  const entries=items.map(e=>({sessionId:e.sessionId,relative:e.relative,files:[e.v3,e.v4].filter(Boolean).map(f=>({name:f.name,sha256:f.sha256,bytes:f.size}))}));
  if(entries.some(e=>!validEntry(e)))I.fail('RUNTIME_SNAPSHOT_INVALID');
  const receiptSource=path.join(stateRoot,'.history-v4-incremental-r29','manifest.json');
  let receipt;if(await I.exists(receiptSource)){const raw=await I.rawFile(receiptSource,8*1024*1024);receipt={sha256:raw.sha256,bytes:raw.bytes.length};}
  record={schema:'owned-runtime-020-snapshot-v1',runtime:'0.2.0-rc.2',status:'preparing',items:entries,...(receipt?{receipt}:{})};
  await I.writeManifest(root,record);await onCheckpoint({phase:'snapshot-planned'});
 }
 // Preserve an older completed migration receipt independently of later batches.
 // It holds identity/hash metadata only, not conversation bodies.
 const receiptDest=path.join(root,RECEIPT_FILE),receiptStage=path.join(root,RECEIPT_STAGE);
 if(record.receipt){
  if(record.status==='complete'){
   if(await I.exists(receiptStage)||(await I.rawFile(receiptDest,8*1024*1024)).sha256!==record.receipt.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
  }else{
   if(await I.exists(receiptDest)&&await I.exists(receiptStage)){
    const a=await lstat(receiptDest,{bigint:true}),b=await lstat(receiptStage,{bigint:true});
    if(a.isSymbolicLink()||b.isSymbolicLink()||!a.isFile()||!b.isFile())I.fail('UNSAFE_FILE');
    if(a.ino===b.ino&&a.dev===b.dev&&a.nlink===2n&&b.nlink===2n){await unlink(receiptStage);await I.syncDirectory(root);}
   }
   if(await I.exists(receiptDest)){
    if(await I.exists(receiptStage)||(await I.rawFile(receiptDest,8*1024*1024)).sha256!==record.receipt.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
   }else{
    const source=await I.rawFile(path.join(stateRoot,'.history-v4-incremental-r29','manifest.json'),8*1024*1024);
    if(source.sha256!==record.receipt.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
    if(await I.exists(receiptStage)){
     const prior=await I.rawFile(receiptStage,8*1024*1024);if(!source.bytes.subarray(0,prior.bytes.length).equals(prior.bytes))I.fail('RUNTIME_SNAPSHOT_CHANGED');await unlink(receiptStage);await I.syncDirectory(root);
    }
    const fd=await open(receiptStage,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|(constants.O_NOFOLLOW||0),0o600);
    try{await fd.writeFile(source.bytes);await fd.sync();}finally{await fd.close();}
    await onCheckpoint({phase:'snapshot-receipt-staged'});await context.fence();
    try{await link(receiptStage,receiptDest);}catch(e){if(e.code==='EEXIST')I.fail('RUNTIME_SNAPSHOT_CHANGED');throw e;}
    await I.syncDirectory(root);await onCheckpoint({phase:'snapshot-receipt-linked'});await unlink(receiptStage);await I.syncDirectory(root);
   }
  }
 }else if(await I.exists(receiptDest)||await I.exists(receiptStage))I.fail('RUNTIME_SNAPSHOT_INVALID');
 await I.safeDirectory(backups);
 if((await readdir(backups)).some(n=>!record.items.some(e=>I.digest(e.sessionId)===n)))I.fail("RUNTIME_SNAPSHOT_INVALID");
 for(const entry of record.items){
  await context.fence();
  const parent=path.join(backups,I.digest(entry.sessionId)),payload=path.join(parent,'payload');
  if(deleted.has(entry.sessionId)){
   if(record.status!=='complete')I.fail('RUNTIME_SNAPSHOT_CHANGED');
   if(await I.safeDirectory(parent,true)&&await I.safeDirectory(payload,true))I.fail('RUNTIME_SNAPSHOT_CHANGED');
   continue;
  }
  if(record.status==='complete'){
   await I.safeDirectory(parent);await I.safeDirectory(payload);
   if((await readdir(parent)).some(n=>n!=='payload'))I.fail('RUNTIME_SNAPSHOT_INVALID');
   const names=await readdir(payload);if(names.length!==entry.files.length||names.some(n=>!entry.files.some(f=>f.name===n)))I.fail('RUNTIME_SNAPSHOT_INVALID');
   for(const f of entry.files)if((await I.rawFile(path.join(payload,f.name),maxBytes)).sha256!==f.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
   continue;
  }
  for(const dir of [parent,payload])if(!await I.safeDirectory(dir,true)){await mkdir(dir,{mode:0o700});await I.syncDirectory(path.dirname(dir));}
  if((await readdir(parent)).some(n=>n!=='payload'&&!entry.files.some(f=>f.name+'.partial'===n)))I.fail('RUNTIME_SNAPSHOT_INVALID');
  for(const f of entry.files){
   const current=items.find(e=>e.sessionId===entry.sessionId),actual=[current?.v3,current?.v4].find(v=>v?.name===f.name);
   if(!actual||actual.sha256!==f.sha256||JSON.stringify(current.relative)!==JSON.stringify(entry.relative))I.fail('RUNTIME_SNAPSHOT_CHANGED');
   const dest=path.join(payload,f.name),stage=path.join(parent,f.name+'.partial');
   if(await I.exists(dest)&&await I.exists(stage)){
    const a=await lstat(dest,{bigint:true}),b=await lstat(stage,{bigint:true});
    if(a.isSymbolicLink()||b.isSymbolicLink()||!a.isFile()||!b.isFile())I.fail('UNSAFE_FILE');
    if(a.ino===b.ino&&a.dev===b.dev&&a.nlink===2n&&b.nlink===2n){await unlink(stage);await I.syncDirectory(parent);}
   }
   if(await I.exists(dest)){
    if((await I.rawFile(dest,maxBytes)).sha256!==f.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
    continue;
   }
   const source=await I.rawFile(path.join(context.sessions,...entry.relative,f.name),maxBytes);
   if(source.sha256!==f.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
   if(await I.exists(stage)){
    const prior=await I.rawFile(stage,maxBytes);if(!source.bytes.subarray(0,prior.bytes.length).equals(prior.bytes))I.fail('RUNTIME_SNAPSHOT_CHANGED');await unlink(stage);await I.syncDirectory(parent);
   }
   const fd=await open(stage,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|(constants.O_NOFOLLOW||0),0o600);
   try{await fd.writeFile(source.bytes);await fd.sync();}finally{await fd.close();}
   await onCheckpoint({phase:'snapshot-staged'});await context.fence();
   try{await link(stage,dest);}catch(e){if(e.code==='EEXIST')I.fail('RUNTIME_SNAPSHOT_CHANGED');throw e;}
   await I.syncDirectory(payload);await onCheckpoint({phase:'snapshot-linked'});await unlink(stage);await I.syncDirectory(parent);
   if((await I.rawFile(path.join(context.sessions,...entry.relative,f.name),maxBytes)).sha256!==f.sha256)I.fail('RUNTIME_SNAPSHOT_CHANGED');
  }
 }
 if(record.status!=='complete'){
  await context.fence();record.status='complete';await I.writeManifest(root,record);await onCheckpoint({phase:'snapshot-complete'});
 }
 return {status:'PRESERVED',sessions:record.items.length};
}
