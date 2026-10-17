/** Offline, incremental V3 -> V4 admission using the pinned rc.2 official codec.
 * Existing V4 and all original V3 bytes are immutable. A separate metadata-only
 * journal owns temporary encoded files, and hard-link publication never replaces
 * a destination. No rollback/delete operation is provided by this module.
 */
import {mkdir,readdir,lstat,open,link,unlink} from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {zstdCompressSync,zstdDecompressSync} from 'node:zlib';
import {createHistoryMaintenance,migrationInternals as I} from './history-native.mjs';
import {preserveRuntimeGeneration} from './history-runtime-snapshot.mjs';

const RUNTIME='0.2.0-rc.2', HOME='.history-v4-incremental-r29';
const sha=x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x);
const segment=x=>typeof x==='string'&&x.length>0&&!['.','..'].includes(x)&&!/[\\/\x00-\x1f:]/.test(x);
const identity=x=>typeof x==='string'&&/^[A-Za-z0-9_-]{1,200}$/.test(x);
const stable=x=>JSON.stringify(x);
const canonical=x=>Array.isArray(x)?x.map(canonical):x&&typeof x==='object'?Object.fromEntries(Object.keys(x).sort().map(k=>[k,canonical(x[k])])):x;
const artifactHash=a=>I.digest(stable(canonical({header:a.header,events:a.events,inheritedEventCount:a.inheritedEventCount})));

export function strictFrames(bytes) {
  const frames=[];let offset=0;
  const need=n=>{if(bytes.length-offset<n)I.fail('MIGRATION_INCOMPLETE_ZSTD');};
  while(offset<bytes.length) {
    const start=offset;need(5);if(bytes.readUInt32LE(offset)!==0xfd2fb528)I.fail('MIGRATION_CORRUPT_ZSTD');offset+=4;
    const d=bytes[offset++];if(d&24)I.fail('MIGRATION_CORRUPT_ZSTD');
    const c=d>>>6,single=!!(d&32),dict=d&3,n=(single?0:1)+(dict===3?4:dict)+(c===0?(single?1:0):1<<c);need(n);offset+=n;
    for(;;){need(3);const b=bytes.readUIntLE(offset,3);offset+=3;const type=(b>>>1)&3;if(type===3)I.fail('MIGRATION_CORRUPT_ZSTD');const size=type===1?1:b>>>3;need(size);offset+=size;if(b&1)break;}
    if(d&4){need(4);offset+=4;}frames.push(bytes.subarray(start,offset));
  }
  if(!frames.length)I.fail('MIGRATION_INCOMPLETE_ZSTD');return frames;
}

export function createHistoryMigration({stateRoot,runtimeRoot,assertQuiescent,maxBytes=32*1024*1024,maxDecodedBytes=128*1024*1024,maxLegacyBytes=8*1024*1024,maxLegacyDecodedBytes=16*1024*1024,maxSessions=2000,onCheckpoint=async()=>{}}) {
  const root=path.resolve(stateRoot),home=path.join(root,HOME),staging=path.join(home,'staging');
  const maintenance=createHistoryMaintenance({stateRoot:root,runtimeRoot,assertQuiescent,maxSessions,maxBytes,maxDecodedBytes});
  const moduleFile=name=>pathToFileURL(path.join(runtimeRoot,'node_modules/@deepseek-ai',name,'lib/index.js')).href;
  const mod=name=>import(moduleFile(name));
  async function json(file,limit) {try{return JSON.parse((await I.rawFile(file,limit)).bytes);}catch(e){if(e.code)throw e;I.fail('MIGRATION_RECEIPT_INVALID');}}
  async function oldReceipt() {
    const dir=path.join(root,'.history-v4-migration');if(!await I.safeDirectory(dir,true))return null;
    const raw=await I.rawFile(path.join(dir,'manifest.json'),4*1024*1024);let r;try{r=JSON.parse(raw.bytes);}catch{I.fail('MIGRATION_RECEIPT_INVALID');}
    if(r.schema!=='owned-session-v4-migration-v1'||r.runtime!=='0.1.7-alpha.2'||!Array.isArray(r.items)||r.items.length>maxSessions||!Array.isArray(r.priorDeletions))I.fail('MIGRATION_RECEIPT_INVALID');
    if(r.status!=='complete')I.fail('MIGRATION_RECEIPT_PENDING');
    const items=new Map();for(const item of r.items){if(!identity(item?.sessionId)||items.has(item.sessionId)||!validRelative(item.relative)||!validFile(item.source,/^session\.v[34]\.jsonl(?:\.zstd)?$/))I.fail('MIGRATION_RECEIPT_INVALID');items.set(item.sessionId,item);}
    return {sha256:raw.sha256,items};
  }
  function validRelative(x){return Array.isArray(x)&&x.length===2&&x.every(segment);}
  function validFile(x,re){return x&&typeof x.name==='string'&&re.test(x.name)&&sha(x.sha256);}
  async function deletions() {
    const dir=path.join(root,'.history-deletions'),ids=new Set();if(!await I.safeDirectory(dir,true))return ids;
    const names=await readdir(dir);if(names.length>20000)I.fail('DELETION_BASELINE_LIMIT');
    for(const name of names){if(!sha(name))I.fail('INVALID_DELETE_MANIFEST');await I.safeDirectory(path.join(dir,name));const v=await json(path.join(dir,name,'manifest.json'),128*1024);
      if(v.version!==1||!identity(v.sessionId)||I.digest(v.sessionId)!==name)I.fail('INVALID_DELETE_MANIFEST');if(v.status!=='deleted')I.fail('MIGRATION_DELETION_PENDING');ids.add(v.sessionId);}
    return ids;
  }
  async function journal(mutate=false) {
    if(!await I.safeDirectory(home,true))return null;
    const names=await readdir(home),orphan=names.filter(x=>/^\.manifest-[a-f0-9-]{36}$/.test(x));
    if(names.some(x=>!['manifest.json','staging'].includes(x)&&!orphan.includes(x)))I.fail('MIGRATION_RECEIPT_INVALID');
    for(const name of orphan){await I.rawFile(path.join(home,name),8*1024*1024);if(!mutate)I.fail('MIGRATION_RECEIPT_PENDING');await unlink(path.join(home,name));}if(orphan.length)await I.syncDirectory(home);
    if(!names.includes('manifest.json')){if(await I.safeDirectory(staging,true)&&(await readdir(staging)).length)I.fail('MIGRATION_STAGING_CONFLICT');return null;}
    const r=await json(path.join(home,'manifest.json'),8*1024*1024);
    if(r.schema!=='owned-session-incremental-v4-v1'||!['0.1.7-rc.2',RUNTIME].includes(r.runtime)||!['publishing','complete'].includes(r.status)||!Array.isArray(r.items)||r.items.length>maxSessions||!Array.isArray(r.baseline)||r.baseline.length>maxSessions||(r.legacy_receipt_sha256!==null&&!sha(r.legacy_receipt_sha256)))I.fail('MIGRATION_RECEIPT_INVALID');
    // A pending older codec transaction must finish with its original runtime.
    if(r.runtime!==RUNTIME && r.status!=='complete')I.fail('MIGRATION_PRIOR_RUNTIME_PENDING');
    const ids=new Set();for(const x of r.items){if(!identity(x?.sessionId)||ids.has(x.sessionId)||!validRelative(x.relative)||!validFile(x.source,/^session\.v3\.jsonl(?:\.zstd)?$/)||!validFile(x.target,/^session\.v4\.jsonl(?:\.zstd)?$/)||x.target.name!==x.source.name.replace('.v3.','.v4.')||!Number.isSafeInteger(x.source.bytes)||x.source.bytes<1||!Number.isSafeInteger(x.target.bytes)||x.target.bytes<1||x.source.bytes>maxBytes||x.target.bytes>maxBytes||!sha(x.artifact_sha256)||!['pending','published'].includes(x.status))I.fail('MIGRATION_RECEIPT_INVALID');ids.add(x.sessionId);}
    const bases=new Set();for(const x of r.baseline){if(!identity(x?.sessionId)||bases.has(x.sessionId)||!sha(x.sha256))I.fail('MIGRATION_RECEIPT_INVALID');bases.add(x.sessionId);}
    if(r.status==='complete'&&r.items.some(x=>x.status!=='published'))I.fail('MIGRATION_RECEIPT_INVALID');
    await I.safeDirectory(staging);const temporary=await readdir(staging);
    const allowed=new Set(r.items.map(x=>I.digest(x.sessionId)+'.stage'));
    if(temporary.some(x=>!allowed.has(x)))I.fail('MIGRATION_STAGING_CONFLICT');
    if(r.status==='complete'&&temporary.length)I.fail('MIGRATION_STAGING_CONFLICT');return r;
  }
  async function physical(file,version) {
    const limit=version===3?Math.min(maxBytes,maxLegacyBytes):maxBytes,decodedLimit=version===3?Math.min(maxDecodedBytes,maxLegacyDecodedBytes):maxDecodedBytes;
    const raw=await I.rawFile(file,limit);let data=raw.bytes;
    if(file.endsWith('.zstd')){const chunks=[];let length=0;for(const frame of strictFrames(data)){const chunk=zstdDecompressSync(frame,{maxOutputLength:decodedLimit-length});length+=chunk.length;if(length>decodedLimit||chunk.at(-1)!==10)I.fail('MIGRATION_INCOMPLETE_LOG');chunks.push(chunk);}data=Buffer.concat(chunks);}if(data.length>decodedLimit)I.fail('ARTIFACT_LIMIT');
    return {...parseBytes(data,version),raw};
  }
  function parseBytes(data,version) {
    if(data.length>maxDecodedBytes||data.at(-1)!==10)I.fail('MIGRATION_INCOMPLETE_LOG');
    let rows;try{rows=new TextDecoder('utf-8',{fatal:true}).decode(data).slice(0,-1).split('\n').map(x=>JSON.parse(x));}catch{I.fail('MIGRATION_INVALID_LOG');}
    const header=rows.shift();if(header?.version!==version)I.fail('MIGRATION_HEADER_MISMATCH');return {header,rows};
  }
  function restore(catalog,p) {const reader=catalog.createRestore(p.header,{recovery:'strict',validation:'current'});for(const row of p.rows)reader.decodeRow(row);return reader.finish();}
  async function strictCurrent(p){const {sessionFormatCatalog}=await mod('dsh-session-format-catalog'),a=restore(sessionFormatCatalog,p);const {Session}=await mod('dsh-session');const s=Session.create(a.header.id,a.events,a.header,a.inheritedEventCount);s.deriveMessages();s.toolHistory();return a;}
  async function converted(context,item,facts) {
    const api=await mod('dsh-session-format-catalog'),p=await physical(path.join(context.sessions,...item.relative,item.v3.name),3);
    if(p.raw.sha256!==item.v3.sha256)I.fail('MIGRATION_SOURCE_CHANGED');
    const children=facts.filter(x=>x.parent===item.sessionId).map(x=>x.value),a=restore(api.createSessionFormatCatalogWithChildren(children),p);
    const encodedHeader=api.sessionFormatCatalog.encodeCurrentHeader(a.header,a.inheritedEventCount);
    if(stable(canonical({...p.header,version:4}))!==stable(canonical(encodedHeader)))I.fail('MIGRATION_HEADER_MISMATCH');
    const text=Buffer.from([encodedHeader,...a.events.map(e=>api.sessionFormatCatalog.encodeCurrentEvent(e))].map(stable).join('\n')+'\n');
    const encoded=item.v3.name.endsWith('.zstd')?zstdCompressSync(text):text;
    if(encoded.length>maxBytes||text.length>maxDecodedBytes)I.fail('FILE_TOO_LARGE');
    const verified=await strictCurrent(parseBytes(text,4));if(artifactHash(a)!==artifactHash(verified))I.fail('MIGRATION_EXPECTED_MISMATCH');
    return {output:encoded,expected:{name:item.v3.name.replace('.v3.','.v4.'),sha256:I.digest(encoded),bytes:encoded.length},artifact_sha256:artifactHash(a)};
  }
  // Recover the only deliberate nlink=2 interval. Never unlink a user path.
  async function recoverLinks(r,context,mutate) {
    if(!r||r.status==='complete')return;
    for(const x of r.items){const temp=path.join(staging,I.digest(x.sessionId)+'.stage'),dest=path.join(context.sessions,...x.relative,x.target.name);
      if(!await I.exists(temp)||!await I.exists(dest))continue;
      const a=await lstat(temp,{bigint:true}),b=await lstat(dest,{bigint:true});
      if(a.isSymbolicLink()||b.isSymbolicLink()||!a.isFile()||!b.isFile())I.fail('UNSAFE_FILE');
      if(a.ino===b.ino&&a.dev===b.dev){if(a.nlink!==2n||b.nlink!==2n||a.size!==BigInt(x.target.bytes))I.fail('MIGRATION_STAGING_CONFLICT');if(!mutate)I.fail('MIGRATION_RECEIPT_PENDING');await context.fence();await unlink(temp);await I.syncDirectory(staging);}
    }
  }
  async function inspect(context,r,{mutate=false}={}) {
    const old=await oldReceipt(),deleted=await deletions();if(r&&(r.legacy_receipt_sha256!==old?.sha256&&!(r.legacy_receipt_sha256===null&&!old)))I.fail('MIGRATION_SOURCE_CHANGED');
    await recoverLinks(r,context,mutate);
    const inventory=await context.deletionInventory();if(!inventory.complete)I.fail('INCOMPLETE_DEPENDENCY_INVENTORY');
    const api=await mod('dsh-session-format-catalog'),converter=await mod('dsh-session-format-v3-to-v4');
    const proof=new Map((r?.items||[]).map(x=>[x.sessionId,x])),items=[],facts=[];let bytes=0,events=0;
    const converting=new Set(inventory.sessions.filter(x=>x.header.version===3||proof.get(x.sessionId)?.status==='pending').map(x=>x.sessionId));
    function childEvidence(info,a){
      if(info.header.origin!=='subagent'||!converting.has(info.header.parentSession))return;
      const source=converter.historicalChildCatalogSource(a),d=source.descriptor;
      // The official childCatalogFact consumes only these descriptor fields.
      // Do not retain optional agent prompts or extension payloads in memory.
      if(d&&typeof d==='object'&&!Array.isArray(d))source.descriptor=Object.fromEntries(['version','provider','mode','label'].filter(k=>Object.hasOwn(d,k)).map(k=>[k,d[k]]));
      facts.push({parent:info.header.parentSession,value:source});
    }
    for(const info of inventory.sessions.sort((a,b)=>a.sessionId.localeCompare(b.sessionId))){await context.fence();if(deleted.has(info.sessionId))I.fail('SESSION_REAPPEARED');
      const dir=path.join(context.sessions,...info.relative),snap=await I.deletionSnapshot(dir),v3=snap.files.find(x=>x.name.startsWith('session.v3.')),v4=snap.files.find(x=>x.name.startsWith('session.v4.'));
      const saved=proof.get(info.sessionId),prior=old?.items.get(info.sessionId);
      if(saved&&(!v3||stable(saved.relative)!==stable(info.relative)||saved.source.name!==v3.name||saved.source.sha256!==v3.sha256))I.fail('MIGRATION_SOURCE_CHANGED');
      if(v3&&v4&&!saved&&(!prior||stable(prior.relative)!==stable(info.relative)||prior.source.name!==v3.name||prior.source.sha256!==v3.sha256))I.fail('DUAL_GENERATION_WITHOUT_RECEIPT');
      const fact={...info,v3,v4,snap};
      // Only compact descriptor evidence escapes this iteration; neither full
      // event arrays nor raw history buffers are retained across the corpus.
      if(v4){const p=await physical(path.join(dir,v4.name),4),a=await strictCurrent(p);bytes+=p.raw.bytes.length;events+=a.events.length;if(saved&&r.status==='publishing'&&!r.baseline.some(x=>x.sessionId===saved.sessionId)&&p.raw.sha256!==saved.target.sha256)I.fail('MIGRATION_PUBLISHED_CONFLICT');childEvidence(info,a);}
      else if(v3){const p=await physical(path.join(dir,v3.name),3),a=restore(api.historicalSessionFormatCatalog,p);bytes+=p.raw.bytes.length;events+=a.events.length;childEvidence(info,a);}
      if(!v3&&!v4)I.fail('INCOMPLETE_DEPENDENCY_INVENTORY');if(bytes>256*1024*1024)I.fail('MIGRATION_CORPUS_LIMIT');if(events>1000000)I.fail('MIGRATION_EVENT_LIMIT');items.push(fact);
    }
    for(const saved of proof.values())if(!items.some(x=>x.sessionId===saved.sessionId)&&(!deleted.has(saved.sessionId)||saved.status!=='published'||r.status!=='complete'))I.fail('MIGRATION_SOURCE_CHANGED');
    // Whole inventory supplies every direct child's descriptor. V4 children
    // use their strictly restored current artifact; legacy sources stay fixed.
    for(const item of items.filter(x=>x.v3&&!x.v4||proof.get(x.sessionId)?.status==='pending')){
      const {expected,artifact_sha256}=await converted(context,item,facts);item.expected=expected;item.artifact_sha256=artifact_sha256;
      const saved=proof.get(item.sessionId);if(saved&&(stable(saved.target)!==stable(item.expected)||saved.artifact_sha256!==item.artifact_sha256))I.fail('MIGRATION_EXPECTED_MISMATCH');
    }
    if(r?.status==='publishing')for(const base of r.baseline){const item=items.find(x=>x.sessionId===base.sessionId);if(!item?.v4||item.v4.sha256!==base.sha256)I.fail('MIGRATION_SOURCE_CHANGED');}
    for(const x of items)if((await I.deletionSnapshot(path.join(context.sessions,...x.relative))).digest!==x.snap.digest)I.fail('SNAPSHOT_CHANGED');
    return {items,facts,old,deleted,bytes,events};
  }
  async function publish(context,r,item,facts) {
    const record=r.items.find(x=>x.sessionId===item.sessionId),dest=path.join(context.sessions,...record.relative,record.target.name),temp=path.join(staging,I.digest(item.sessionId)+'.stage');
    const prepared=await converted(context,item,facts),output=prepared.output;if(stable(prepared.expected)!==stable(record.target)||prepared.artifact_sha256!==record.artifact_sha256)I.fail('MIGRATION_EXPECTED_MISMATCH');
    if(await I.exists(dest)){if((await I.rawFile(dest,maxBytes)).sha256!==record.target.sha256)I.fail('MIGRATION_PUBLISHED_CONFLICT');if(await I.exists(temp)){const bytes=(await I.rawFile(temp,maxBytes)).bytes;if(!bytes.equals(output))I.fail('MIGRATION_STAGING_CONFLICT');await unlink(temp);await I.syncDirectory(staging);}}
    else {
      if(await I.exists(temp)){const previous=(await I.rawFile(temp,maxBytes)).bytes;if(!output.subarray(0,previous.length).equals(previous))I.fail('MIGRATION_STAGING_CONFLICT');if(!previous.equals(output)){await context.fence();await unlink(temp);await I.syncDirectory(staging);}}
      if(!await I.exists(temp)){const handle=await open(temp,constants.O_WRONLY|constants.O_CREAT|constants.O_EXCL|(constants.O_NOFOLLOW||0),0o600);try{await handle.writeFile(output);await handle.sync();}finally{await handle.close();}await I.syncDirectory(staging);}
      await onCheckpoint({phase:'after-stage'});await context.fence();
      if((await I.rawFile(path.join(context.sessions,...record.relative,record.source.name),maxBytes)).sha256!==record.source.sha256)I.fail('MIGRATION_SOURCE_CHANGED');
      // link(2) is atomic and refuses any existing destination, including symlinks.
      try{await link(temp,dest);}catch(e){if(e.code==='EEXIST')I.fail('MIGRATION_PUBLISHED_CONFLICT');throw e;}
      await I.syncDirectory(path.dirname(dest));await onCheckpoint({phase:'after-link'});
      await unlink(temp);await I.syncDirectory(staging);await onCheckpoint({phase:'after-unlink'});
    }
    record.status='published';await I.writeManifest(home,r);await onCheckpoint({phase:'after-item'});
  }
  async function execute(context,mutate) {
    let r=await journal(mutate),scan=await inspect(context,r,{mutate});const needs=scan.items.filter(x=>x.v3&&!x.v4);
    if(!mutate)return {status:'VERIFIED',runtime:RUNTIME,format:4,sessions:scan.items.length,migrations:needs.length,journal_pending:r?.status==='publishing',liveChanges:'NONE'};
    await preserveRuntimeGeneration({stateRoot:root,context,items:scan.items,deleted:scan.deleted,maxBytes,onCheckpoint});
    if(needs.length&&(!r||r.status==='complete')){
      // Completed deletion tombstones were verified by inspect; their metadata
      // no longer authorizes a physical generation and must not enter a new
      // pending batch (nor accumulate against the active-session limit).
      const oldItems=(r?.items||[]).filter(x=>!scan.deleted.has(x.sessionId)),ids=new Set(oldItems.map(x=>x.sessionId));
      const additions=needs.map(x=>{if(ids.has(x.sessionId))I.fail('MIGRATION_SOURCE_CHANGED');return {sessionId:x.sessionId,relative:x.relative,source:{name:x.v3.name,sha256:x.v3.sha256,bytes:x.v3.size},target:x.expected,artifact_sha256:x.artifact_sha256,status:'pending'};});
      if(oldItems.length+additions.length>maxSessions)I.fail('SESSION_LIMIT');
      r={schema:'owned-session-incremental-v4-v1',runtime:RUNTIME,status:'publishing',legacy_receipt_sha256:scan.old?.sha256??null,baseline:scan.items.filter(x=>x.v4).map(x=>({sessionId:x.sessionId,sha256:x.v4.sha256})),items:[...oldItems,...additions]};
      if(!await I.safeDirectory(home,true)){await mkdir(home,{mode:0o700});await I.syncDirectory(root);}if(!await I.safeDirectory(staging,true)){await mkdir(staging,{mode:0o700});await I.syncDirectory(home);}await I.writeManifest(home,r);await onCheckpoint({phase:'after-plan'});
    }
    if(r?.status==='publishing'){
      scan=await inspect(context,r,{mutate:true});
      for(const item of scan.items.filter(x=>r.items.find(y=>y.sessionId===x.sessionId)?.status==='pending'))await publish(context,r,item,scan.facts);
      await inspect(context,r,{mutate:true});if((await readdir(staging)).length)I.fail('MIGRATION_STAGING_CONFLICT');r.status='complete';await I.writeManifest(home,r);await onCheckpoint({phase:'after-complete'});
    }
    const final=await inspect(context,r,{mutate:true});if(final.items.some(x=>!x.v4))I.fail('LEGACY_MIGRATION_REQUIRED');
    const admission=path.join(root,'.history-runtime-admission');if(!await I.safeDirectory(admission,true)){await mkdir(admission,{mode:0o700});await I.syncDirectory(root);}if(await I.exists(path.join(admission,'manifest.json')))await I.rawFile(path.join(admission,'manifest.json'),128*1024);
    await I.writeManifest(admission,{schema:'owned-session-runtime-admission-v1',status:'complete',runtime:RUNTIME,format:4,sessions:final.items.length,legacy_receipt_sha256:final.old?.sha256??null,incremental_receipt_sha256:r?I.digest(stable(r)+'\n'):null});
    return {status:'READY',runtime:RUNTIME,format:4};
  }
  return {check:()=>maintenance.exclusive(c=>execute(c,false)),admit:()=>maintenance.exclusive(c=>execute(c,true)),migrate:()=>maintenance.exclusive(c=>execute(c,true)),rollback:()=>I.fail('RUNTIME_SNAPSHOT_REQUIRED')};
}
