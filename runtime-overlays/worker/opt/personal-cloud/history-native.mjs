/** Reviewed candidate for exactly DSH 0.2.0-rc.2, Session V4.
 * Offline manager fence remains mandatory. Permanent deletion removes all
 * validated session generations plus its private migration backup, preserving
 * shared attachments/project files. Legacy restore/quarantine is not exposed.
 */
import { constants } from 'node:fs';
import { lstat, readdir, realpath, open, mkdir, rename, unlink, rmdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import { zstdDecompressSync, createZstdDecompress } from 'node:zlib';
import { Readable } from 'node:stream';

const DAY = 86400000;
// Runtime compatibility and persisted format are separate contracts. This
// exact release is audited for V4; newer packages require explicit review.
const SUPPORTED_RUNTIMES = new Set(['0.2.0-rc.2']);
const FILES = new Set(['session.v3.jsonl.zstd', 'session.v3.jsonl', 'session.v4.jsonl.zstd', 'session.v4.jsonl', 'session.lock']);
function validGenerations(names) {
  const logs=names.filter(x=>x!=='session.lock');
  return logs.length>=1 && logs.length<=2 && logs.every(x=>FILES.has(x))
    && new Set(logs.map(x=>x.split('.')[1])).size===logs.length
    && new Set(logs.map(x=>x.endsWith('.zstd'))).size===1;
}
const digest = value => createHash('sha256').update(value).digest('hex');
const fail = code => { throw new HistoryRefusal(code); };
export class HistoryRefusal extends Error {
  constructor(code) { super(code); this.name = 'HistoryRefusal'; this.code = code; }
}
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
// JSON object property order is not history semantics. Keep every field in
// this comparison, including unknown metadata, while ignoring object order.
const canonicalHeader=value=>Array.isArray(value)?value.map(canonicalHeader):plain(value)?Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonicalHeader(value[key])])):value;
const segment = value => typeof value === 'string' && value.length > 0 && value !== '.' && value !== '..'
  && !/[\\/\x00-\x1f:]/u.test(value);
const stamp = stat => ({ size: Number(stat.size), mtimeNs: String(stat.mtimeNs),
  ctimeNs: String(stat.ctimeNs), ino: String(stat.ino), dev: String(stat.dev) });

async function safeDirectory(value, allowMissing = false) {
  const absolute = path.resolve(value);
  let cursor = path.parse(absolute).root;
  for (const part of absolute.slice(cursor.length).split(path.sep).filter(Boolean)) {
    cursor = path.join(cursor, part);
    let stat;
    try { stat = await lstat(cursor); } catch (error) {
      if (allowMissing && error.code === 'ENOENT' && cursor === absolute) return undefined;
      throw error;
    }
    if (stat.isSymbolicLink() || !stat.isDirectory()) fail('UNSAFE_DIRECTORY');
  }
  return realpath(absolute);
}
async function exists(value) {
  try { await lstat(value); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
async function syncDirectory(value) {
  // Windows rename is atomic but directory fsync isn't supported by Node.
  if (process.platform === 'win32') return;
  const file = await open(value, 'r');
  try { await file.sync(); } finally { await file.close(); }
}
async function rawFile(file, limit) {
  const before = await lstat(file, { bigint: true });
  if (!before.isFile() || before.isSymbolicLink() || before.nlink !== 1n) fail('UNSAFE_FILE');
  if (before.size > BigInt(limit)) fail('ARTIFACT_LIMIT');
  const handle = await open(file, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const opened = await handle.stat({ bigint: true });
    if (JSON.stringify(stamp(opened)) !== JSON.stringify(stamp(before))) fail('SNAPSHOT_CHANGED');
    const bytes = await handle.readFile();
    const after = await handle.stat({ bigint: true });
    if (JSON.stringify(stamp(after)) !== JSON.stringify(stamp(before))) fail('SNAPSHOT_CHANGED');
    return { ...stamp(after), sha256: digest(bytes), bytes };
  } finally { await handle.close(); }
}
// Explicit user deletion needs file identity, not a decoded copy of a large
// conversation. Hash in fixed chunks and refuse changed/redirected handles.
async function fileFingerprint(file) {
  const before=await lstat(file,{bigint:true});
  if(!before.isFile()||before.isSymbolicLink()||before.nlink!==1n)fail('UNSAFE_FILE');
  const handle=await open(file,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
  try {
    if(JSON.stringify(stamp(await handle.stat({bigint:true})))!==JSON.stringify(stamp(before)))fail('SNAPSHOT_CHANGED');
    const hash=createHash('sha256'),buffer=Buffer.alloc(65536);
    for(;;){const {bytesRead}=await handle.read(buffer,0,buffer.length,null);if(!bytesRead)break;hash.update(buffer.subarray(0,bytesRead));}
    const after=await handle.stat({bigint:true});
    if(JSON.stringify(stamp(after))!==JSON.stringify(stamp(before)))fail('SNAPSHOT_CHANGED');
    return {...stamp(after),sha256:hash.digest('hex')};
  } finally {await handle.close();}
}
async function deletionSnapshot(directory) {
  await safeDirectory(directory);
  const names=(await readdir(directory)).sort();
  if(names.length<1||names.length>3||names.some(name=>!FILES.has(name))||!validGenerations(names))fail('UNRECOGNIZED_ARTIFACTS');
  const files=[];for(const name of names)files.push({name,...await fileFingerprint(path.join(directory,name))});
  return {files,digest:digest(JSON.stringify(files))};
}
async function deletionHeader(file) {
  const before=await lstat(file,{bigint:true});
  if(!before.isFile()||before.isSymbolicLink()||before.nlink!==1n)fail('UNSAFE_FILE');
  const handle=await open(file,constants.O_RDONLY|(constants.O_NOFOLLOW||0));
  let prefix;
  try {
    if(JSON.stringify(stamp(await handle.stat({bigint:true})))!==JSON.stringify(stamp(before)))fail('SNAPSHOT_CHANGED');
    const buffer=Buffer.alloc(65536),{bytesRead}=await handle.read(buffer,0,buffer.length,0);prefix=buffer.subarray(0,bytesRead);
    if(JSON.stringify(stamp(await handle.stat({bigint:true})))!==JSON.stringify(stamp(before)))fail('SNAPSHOT_CHANGED');
  } finally {await handle.close();}
  let line;
  if(file.endsWith('.zstd')) {
    // Stop after the first line. Stream chunks and output cap also bound a
    // malformed compressed header; never expand the body merely to delete it.
    const input=Readable.from([prefix]),decoder=createZstdDecompress({chunkSize:16384});input.pipe(decoder);
    const parts=[];let size=0;
    try {
      for await(const chunk of decoder) {
        const newline=chunk.indexOf(10),part=newline<0?chunk:chunk.subarray(0,newline);
        size+=part.length;if(size>65536)fail('HEADER_LIMIT');parts.push(part);
        if(newline>=0){line=Buffer.concat(parts).toString('utf8');break;}
      }
    } finally {input.destroy();decoder.destroy();}
  } else {const newline=prefix.indexOf(10);if(newline>=0)line=prefix.subarray(0,newline).toString('utf8');}
  if(line===undefined)fail('HEADER_LIMIT');
  const header=JSON.parse(line);
  if(header?.type!=='session'||![3,4].includes(header.version)||!/^[A-Za-z0-9_-]{1,200}$/.test(header.id||'')
      ||typeof header.isSeeded!=='boolean'||!Number.isSafeInteger(header.createdAt)||header.createdAt<0
      ||!Number.isSafeInteger(header.delegationDepth)||header.delegationDepth<0
      ||(header.cwd!==undefined&&(typeof header.cwd!=='string'||!path.isAbsolute(header.cwd)))
      ||(header.parentSession!==undefined&&!/^[A-Za-z0-9_-]{1,200}$/.test(header.parentSession)))fail('UNSUPPORTED_FORMAT');
  return header;
}
async function directorySnapshot(directory, maxBytes, includeRaw = false) {
  await safeDirectory(directory);
  const names = (await readdir(directory)).sort();
  if (names.length < 1 || names.length > 2 || names.some(name => !FILES.has(name))) fail('UNRECOGNIZED_ARTIFACTS');
  const logs = names.filter(name => name !== 'session.lock');
  if (logs.length !== 1) fail('UNRECOGNIZED_GENERATION');
  const files = [];
  let log;
  for (const name of names) {
    const { bytes, ...metadata } = await rawFile(path.join(directory, name), maxBytes);
    files.push({ name, ...metadata, ...(includeRaw ? { bytes } : {}) });
    if (name !== 'session.lock') log = { name, bytes };
  }
  const fingerprint = files.map(({ bytes, ...value }) => value);
  return { files, digest: digest(JSON.stringify(fingerprint)), log };
}
async function writeManifest(dir, value, create = false) {
  const temporary = path.join(dir, '.manifest-' + randomUUID());
  const handle = await open(temporary, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(value) + '\n'); await handle.sync(); } finally { await handle.close(); }
  try {
    if (create && await exists(path.join(dir, 'manifest.json'))) fail('MANIFEST_EXISTS');
    await rename(temporary, path.join(dir, 'manifest.json'));
    await syncDirectory(dir);
  } catch (error) { await unlink(temporary).catch(() => {}); throw error; }
}

async function upstream(runtimeRoot) {
  const base = path.resolve(runtimeRoot, 'node_modules', '@deepseek-ai');
  let runtimeVersion;
  for (const name of ['dsh-session', 'dsh-session-persistence-jsonl']) {
    const pkg = JSON.parse(await readFile(path.join(base, name, 'package.json'), 'utf8'));
    if (!SUPPORTED_RUNTIMES.has(pkg.version) || (runtimeVersion && pkg.version !== runtimeVersion)) fail('UNSUPPORTED_RUNTIME');
    runtimeVersion = pkg.version;
  }
  const [{ Context }, { default: Backend }, { SESSION_FORMAT_VERSION, KNOWN_SESSION_EVENT_TYPES }] = await Promise.all([
    import(pathToFileURL(path.join(base, 'cordis/lib/index.js')).href),
    import(pathToFileURL(path.join(base, 'dsh-session-persistence-jsonl/lib/index.js')).href),
    import(pathToFileURL(path.join(base, 'dsh-session/lib/index.js')).href),
  ]);
  if (SESSION_FORMAT_VERSION !== 4) fail('UNSUPPORTED_FORMAT');
  return { Context, Backend, knownEventTypes: KNOWN_SESSION_EVENT_TYPES, runtimeVersion };
}
function references(value, ids, output) {
  if (typeof value === 'string') { if (ids.has(value)) output.add(value); return; }
  if (Array.isArray(value)) { for (const item of value) references(item, ids, output); return; }
  if (plain(value)) for (const item of Object.values(value)) references(item, ids, output);
}
function relationshipFields(value) {
  if (Array.isArray(value)) return value.some(relationshipFields);
  if (!plain(value)) return false;
  return Object.entries(value).some(([key, item]) => /(?:session.?id|parent.?session|child.?session|agent.?id)/iu.test(key)
    || relationshipFields(item));
}

// Use the shipped schema/backend for the workspace registry. Only the exact
// archived session's membership and disposable projection records are touched;
// project files and content-addressed attachments can be shared and stay put.
async function deleteMetadata(root, runtimeRoot, sessionId, dryRun, limit, preserve = []) {
  const storages = path.join(root, 'dsh', 'storages');
  if (!(await safeDirectory(storages, true))) { if(preserve.length)fail('SESSION_DEPENDENCY');return; }
  const base = path.join(runtimeRoot, 'node_modules', '@deepseek-ai');
  const [{ JsonStorageBackend }, { descriptorOf }, { workspaceDomainSpec }] = await Promise.all([
    import(pathToFileURL(path.join(base, 'dsh-storage-json/lib/index.js')).href),
    import(pathToFileURL(path.join(base, 'dsh-storage-domain/lib/index.js')).href),
    import(pathToFileURL(path.join(base, 'dsh-workspace/lib/index.js')).href),
  ]);
  if (workspaceDomainSpec?.name !== 'workspace' || workspaceDomainSpec.version !== 2) fail('UNSUPPORTED_REGISTRY');
  const registry = path.join(storages, 'workspace.json');
  if (preserve.length && !await exists(registry)) fail('SESSION_DEPENDENCY');
  if (await exists(registry)) {
    await rawFile(registry, limit);
    const backend = new JsonStorageBackend(storages);
    try {
      const unit = await backend.kv.open(descriptorOf(workspaceDomainSpec)), snapshot = await unit.loadAll();
      const state = workspaceDomainSpec.global.schema.parse(snapshot.global);
      if (state.pendingMutation) fail('REGISTRY_MUTATION_PENDING');
      if(!state.initialized || new Set(state.workspaceIds).size!==state.workspaceIds.length
          || Object.keys(snapshot.tables.workspaces).length!==state.workspaceIds.length
          || state.workspaceIds.some(id=>!Object.hasOwn(snapshot.tables.workspaces,id)))fail('UNSAFE_REGISTRY');
      for (const value of Object.values(snapshot.tables.workspaces)) workspaceDomainSpec.tables.workspaces.valueSchema.parse(value);
      const accounted=new Set();
      for(const value of Object.values(snapshot.tables.workspaces))for(const id of value.sessionIds) {
        if(accounted.has(id))fail('UNSAFE_REGISTRY');accounted.add(id);
      }
      const updates=new Map();
      for(const child of preserve) {
        if(!/^[A-Za-z0-9_-]{1,200}$/.test(child.id||'') || typeof child.cwd!=='string' || !path.isAbsolute(child.cwd))fail('SESSION_DEPENDENCY');
        const direct=Object.entries(snapshot.tables.workspaces).filter(([,record])=>record.sessionIds.includes(child.id)&&record.path===child.cwd);
        if(direct.length===1)continue;
        if(direct.length>1)fail('SESSION_DEPENDENCY');
        if(accounted.has(child.id))fail('SESSION_DEPENDENCY');
        const candidates=Object.entries(snapshot.tables.workspaces).filter(([,record])=>record.path===child.cwd);
        if(candidates.length!==1)fail('SESSION_DEPENDENCY');
        const [key,record]=candidates[0],value=updates.get(key)||record;
        updates.set(key,{...value,sessionIds:[...value.sessionIds,child.id]});
      }
      if (!dryRun) {
        for (const [key, original] of Object.entries(snapshot.tables.workspaces)) {
          const value=updates.get(key)||original;
          if(value!==original || value.sessionIds.includes(sessionId))
            await unit.putRecord('workspaces', key, {...value, sessionIds:value.sessionIds.filter(id=>id!==sessionId)});
        }
        if (state.archivedSessionIds.includes(sessionId) || state.pinnedSessionIds.includes(sessionId))
          await unit.setGlobal({...snapshot.global, archivedSessionIds:state.archivedSessionIds.filter(id=>id!==sessionId), pinnedSessionIds:state.pinnedSessionIds.filter(id=>id!==sessionId)});
      }
    } finally { await backend.close(); }
  }
  // A current checkpoint and any schema-invalid backup of this exact record
  // can contain titles/message projections. Keep all other sessions' caches.
  const cache = path.join(storages, 'session_projcache');
  if (await safeDirectory(cache, true)) {
    const records = path.join(cache, 'sessions');
    if (await safeDirectory(records, true)) {
      const names = await readdir(records);
      if (names.length > 20000) fail('PROJECTION_LIMIT');
      for (const name of names) if (name === sessionId+'.json' || name.startsWith(sessionId+'.json.bak.')) {
        if (!segment(name)) fail('UNSAFE_FILE');
        await fileFingerprint(path.join(records,name));
        if (!dryRun) await unlink(path.join(records,name));
      }
      if (!dryRun) await syncDirectory(records);
    }
  }
  // Older versions used a single disposable checkpoint file. Invalidate that
  // derived cache as a whole (retained logs rebuild it), without decoding a
  // potentially enormous legacy conversation projection into memory.
  const legacy = path.join(storages,'session_projcache.json');
  if (await exists(legacy)) {
    await fileFingerprint(legacy);
    if(!dryRun){await unlink(legacy);await syncDirectory(storages);}
  }
}

/** Returns one maintenance capability scoped to exactly one owned state volume. */
export function createHistoryMaintenance({ stateRoot, runtimeRoot = '/opt/dsh', assertQuiescent,
  confirmArchived, now = Date.now, retentionDays = 90, trashDays = 30,
  maxSessions = 10000, maxBytes = 32 * 1024 * 1024, maxDecodedBytes = 128 * 1024 * 1024,
  maxTotalBytes = 64 * 1024 * 1024, maxTotalEvents = 100000 } = {}) {
  if (typeof stateRoot !== 'string' || !path.isAbsolute(stateRoot) || typeof assertQuiescent !== 'function'
    || retentionDays !== 90 || trashDays !== 30 || !Number.isSafeInteger(maxSessions) || maxSessions < 1
    || !Number.isSafeInteger(maxBytes) || maxBytes < 1 || !Number.isSafeInteger(maxDecodedBytes) || maxDecodedBytes < 1
    || !Number.isSafeInteger(maxTotalBytes) || maxTotalBytes < 1 || !Number.isSafeInteger(maxTotalEvents) || maxTotalEvents < 1) fail('INVALID_OPTIONS');
  const root = path.resolve(stateRoot), sessions = path.join(root, 'dsh', 'sessions');
  const trash = path.join(root, '.history-trash');
  async function fence() {
    const value = await assertQuiescent();
    if (value?.stopped !== true || value?.exclusive !== true) fail('NATIVE_NOT_QUIESCENT');
    await safeDirectory(root);
  }
  async function locked(fn) {
    await fence();
    // Refuse an unreviewed runtime before loading even its lock addon.
    await upstream(runtimeRoot);
    const lockPath = path.join(root, '.history-maintenance.lock');
    let lock, removeLock = process.platform === 'win32';
    try {
      if (removeLock) {
        // Windows is useful for synthetic codec tests only. A crash leaves a
        // fail-closed stale marker there. Production maintenance runs on Linux.
        lock = await open(lockPath, 'wx', 0o600);
      } else {
        // Kernel flock is released even on SIGKILL/OOM. Keep its inode forever
        // so an interrupted purge remains recoverable without breaking a lock.
        const { tryLockExclusive } = await import(pathToFileURL(path.join(runtimeRoot,
          'node_modules/@deepseek-ai/node-addon-system/lib/flock.js')).href);
        lock = await open(lockPath, constants.O_RDWR | constants.O_CREAT | (constants.O_NOFOLLOW || 0), 0o600);
        await tryLockExclusive(lock.fd);
        const held = await lock.stat({ bigint: true }), current = await lstat(lockPath, { bigint: true });
        if (!held.isFile() || held.nlink !== 1n || current.isSymbolicLink() || held.ino !== current.ino || held.dev !== current.dev) fail('UNSAFE_MAINTENANCE_LOCK');
      }
    } catch (error) {
      await lock?.close();
      if (['EEXIST', 'EAGAIN', 'EWOULDBLOCK'].includes(error.code)) fail('MAINTENANCE_LOCKED'); throw error;
    }
    try { await fence(); return await fn(); }
    finally { await lock.close(); if (removeLock) await unlink(lockPath); }
  }
  async function receipt(sessionId, expectedDigest) {
    if (typeof confirmArchived !== 'function') fail('ARCHIVE_NOT_CONFIRMED');
    const result = await confirmArchived({ sessionId, digest: expectedDigest });
    if (result?.stored !== true || result.digest !== expectedDigest) fail('ARCHIVE_NOT_CONFIRMED');
  }
  async function trashManifest(trashId) {
    if (typeof trashId !== 'string' || !/^[a-f0-9-]{36}$/u.test(trashId)) fail('INVALID_TRASH_ID');
    await safeDirectory(trash);
    const dir = path.join(trash, trashId);
    await safeDirectory(dir);
    const metadata = await rawFile(path.join(dir, 'manifest.json'), 128 * 1024);
    let manifest;
    try { manifest = JSON.parse(metadata.bytes); } catch { fail('INVALID_MANIFEST'); }
    if (manifest.version !== 1 || manifest.trashId !== trashId || !Array.isArray(manifest.relative)
      || manifest.relative.length !== 2 || !manifest.relative.every(segment) || typeof manifest.sessionId !== 'string'
      || !/^[a-f0-9]{64}$/u.test(manifest.digest) || !Number.isSafeInteger(manifest.trashedAt)
      || manifest.purgeAfter !== manifest.trashedAt + trashDays * DAY
      || (manifest.restoredAt !== undefined && !Number.isSafeInteger(manifest.restoredAt))
      || (manifest.restoringAt !== undefined && !Number.isSafeInteger(manifest.restoringAt))
      || !Array.isArray(manifest.files) || manifest.files.length < 1 || manifest.files.length > 2
      || manifest.files.some(file => !plain(file) || !FILES.has(file.name))
      || digest(JSON.stringify(manifest.files)) !== manifest.digest
      || !['prepared', 'trashed', 'restoring', 'restored', 'purging', 'purged'].includes(manifest.status)) fail('INVALID_MANIFEST');
    return { dir, manifest };
  }
  const match = (actual, expected) => { if (!/^[a-f0-9]{64}$/u.test(expected || '') || actual !== expected) fail('SNAPSHOT_CHANGED'); };
  async function deletionInventory(targetId) {
    if(!await safeDirectory(path.join(root,'dsh'),true)||!await safeDirectory(sessions,true))return {sessions:[],complete:true};
    const native=await upstream(runtimeRoot),ctx=new native.Context(),fiber=ctx.plugin(native.Backend,{root:sessions,compression:'zstd'});
    const output=[],ids=new Set();let complete=true,count=0;
    try {
      await fiber.await();const backend=ctx.get('sessionPersistence'),projects=await readdir(sessions);
      if(projects.length>maxSessions)fail('SESSION_LIMIT');
      for(const project of projects) {
        if(!segment(project))fail('UNSAFE_DIRECTORY');
        const projectPath=path.join(sessions,project),projectStat=await lstat(projectPath);
        if(projectStat.isFile()) {complete=false;continue;}
        try {await safeDirectory(projectPath);}catch{complete=false;continue;}
        for(const name of await readdir(projectPath)) {
          if(++count>maxSessions)fail('SESSION_LIMIT');
          try {
            if(!segment(name))fail('UNSAFE_DIRECTORY');
            const directory=path.join(projectPath,name);await safeDirectory(directory);
            const names=(await readdir(directory)).sort();
            if(names.length<1||names.length>3||names.some(x=>!FILES.has(x)))fail('UNRECOGNIZED_ARTIFACTS');
            const logs=names.filter(x=>x!=='session.lock').sort();if(!validGenerations(names))fail('UNRECOGNIZED_GENERATION');
            const headers=await Promise.all(logs.map(x=>deletionHeader(path.join(directory,x))));
            if(headers.some(h=>JSON.stringify(canonicalHeader({...h,version:4}))!==JSON.stringify(canonicalHeader({...headers[0],version:4}))))fail('GENERATION_HEADER_CONFLICT');
            const header=headers.at(-1);
            if(path.dirname(backend.locate(header).path)!==directory||ids.has(header.id))fail('NATIVE_PATH_MISMATCH');
            ids.add(header.id);
            output.push({sessionId:header.id,header,relative:[project,name],
              ...(header.id===targetId?await deletionSnapshot(directory):{})});
          } catch {complete=false;}
        }
      }
    } finally {await fiber.dispose();}
    return {sessions:output,complete};
  }
  async function incrementalMigrationComplete() {
    const directory=path.join(root,'.history-v4-incremental-r29');
    if(!await safeDirectory(directory,true))return;
    let entry;try{entry=JSON.parse((await rawFile(path.join(directory,'manifest.json'),8*1024*1024)).bytes);}catch(error){if(error.code)throw error;fail('MIGRATION_RECEIPT_INVALID');}
    if(entry.schema!=='owned-session-incremental-v4-v1'||!['0.1.7-rc.2','0.2.0-rc.2'].includes(entry.runtime)||!Array.isArray(entry.items)||entry.items.length>maxSessions)fail('MIGRATION_RECEIPT_INVALID');
    if(entry.status!=='complete'||entry.items.some(x=>x?.status!=='published'))fail('MIGRATION_RECEIPT_PENDING');
    await safeDirectory(path.join(directory,'staging'));
    if((await readdir(path.join(directory,'staging'))).length)fail('MIGRATION_STAGING_CONFLICT');
  }
  async function deleteSession(sessionId) {
    await incrementalMigrationComplete();
    const runtimeSnapshot=path.join(root,'.history-runtime-020-snapshot');
    if(await safeDirectory(runtimeSnapshot,true)){
      let snapshot;try{snapshot=JSON.parse((await rawFile(path.join(runtimeSnapshot,'manifest.json'),8*1024*1024)).bytes);}catch(error){if(error.code)throw error;fail('RUNTIME_SNAPSHOT_INVALID');}
      if(snapshot?.schema!=='owned-runtime-020-snapshot-v1'||snapshot.runtime!=='0.2.0-rc.2')fail('RUNTIME_SNAPSHOT_INVALID');
      if(snapshot.status!=='complete')fail('RUNTIME_SNAPSHOT_PENDING');
    }
    if (!/^[A-Za-z0-9_-]{1,200}$/.test(sessionId || '')) fail('INVALID_SESSION_ID');
    const native = await upstream(runtimeRoot);
    if (native.runtimeVersion !== '0.2.0-rc.2') fail('UNSUPPORTED_RUNTIME');
    const ctx=new native.Context(), fiber=ctx.plugin(native.Backend,{root:sessions,compression:'zstd'});
    let leaf;
    try { await fiber.await();leaf=path.basename(path.dirname(ctx.get('sessionPersistence').locate({id:sessionId,cwd:'/workspace'}).path)); }
    finally { await fiber.dispose(); }
    const journals=path.join(root,'.history-deletions'), journal=path.join(journals,digest(sessionId));
    let entry;
    if (await safeDirectory(journals,true) && await safeDirectory(journal,true)) {
      entry=JSON.parse((await rawFile(path.join(journal,'manifest.json'),128*1024)).bytes);
      if (entry.version!==1 || entry.sessionId!==sessionId || !['deleting','deleted'].includes(entry.status)
          || !Array.isArray(entry.sources) || entry.sources.length>8 || !Array.isArray(entry.preserve)
          || entry.preserve.length>maxSessions) fail('INVALID_DELETE_MANIFEST');
    } else {
      const inventory=await deletionInventory(sessionId);
      if (!inventory.complete) return {status:'deferred',sessionId,reason:'INCOMPLETE_DEPENDENCY_INVENTORY'};
      // Reviewed V4 stores the complete inherited event prefix in each
      // child. Parent ids are lineage, not storage indirection. The only
      // ancestor-based API resolution is subagent fork workspace selection;
      // give every retained direct child its own validated membership first.
      const preserve=inventory.sessions.filter(x=>x.sessionId!==sessionId&&x.header.parentSession===sessionId)
        .map(x=>({id:x.sessionId,cwd:x.header.cwd}));
      const item=inventory.sessions.find(x=>x.sessionId===sessionId), sources=[];
      if (item) sources.push({kind:'sessions',relative:item.relative,files:item.files,digest:item.digest});
      if (await safeDirectory(trash,true)) {
        const names=await readdir(trash);if(names.length>maxSessions)fail('TRASH_LIMIT');
        for(const name of names) {
          const {dir,manifest}=await trashManifest(name);
          if(manifest.sessionId!==sessionId || !await exists(path.join(dir,'payload')))continue;
          if(manifest.status!=='purging')match((await deletionSnapshot(path.join(dir,'payload'))).digest,manifest.digest);
          sources.push({kind:'trash',relative:[name,'payload'],files:manifest.files,digest:manifest.digest});
        }
      }
      const backupRoot=path.join(root,'.history-v4-migration','backups');
      const backup=path.join(backupRoot,digest(sessionId),'payload');
      if(await safeDirectory(path.join(root,'.history-v4-migration'),true) && await safeDirectory(backupRoot,true) && await safeDirectory(path.dirname(backup),true) && await safeDirectory(backup,true)) {
        const saved=await deletionSnapshot(backup);
        for(const file of saved.files.filter(x=>x.name!=='session.lock'))if((await deletionHeader(path.join(backup,file.name))).id!==sessionId)fail('MIGRATION_BACKUP_CONFLICT');
        sources.push({kind:'migration',relative:[digest(sessionId),'payload'],files:saved.files,digest:saved.digest});
      }
      const runtimeBackupRoot=path.join(root,'.history-runtime-020-snapshot','backups');
      const runtimeBackup=path.join(runtimeBackupRoot,digest(sessionId),'payload');
      if(await safeDirectory(runtimeSnapshot,true) && await safeDirectory(runtimeBackupRoot,true) && await safeDirectory(path.dirname(runtimeBackup),true) && await safeDirectory(runtimeBackup,true)) {
        const saved=await deletionSnapshot(runtimeBackup);
        for(const file of saved.files.filter(x=>x.name!=='session.lock'))if((await deletionHeader(path.join(runtimeBackup,file.name))).id!==sessionId)fail('RUNTIME_SNAPSHOT_CHANGED');
        sources.push({kind:'runtime-snapshot',relative:[digest(sessionId),'payload'],files:saved.files,digest:saved.digest});
      }
      if(sources.length>8)fail('DELETE_SOURCE_LIMIT');
      try { await deleteMetadata(root,runtimeRoot,sessionId,true,16*1024*1024,preserve); }
      catch(error) { if(error.code==='SESSION_DEPENDENCY')return {status:'deferred',sessionId,reason:error.code};throw error; }
      if(!await safeDirectory(journals,true)){await mkdir(journals,{mode:0o700});await syncDirectory(root);}
      await mkdir(journal,{mode:0o700});await syncDirectory(journals);
      entry={version:1,sessionId,status:'deleting',sources,preserve};
      // No log payload or title is copied. A durable intent makes partial
      // unlink retryable after power loss, while Gateway denies resurrection.
      await writeManifest(journal,entry,true);
    }
    const sourcePath=source=>{
      if(!plain(source) || !['sessions','trash','migration','runtime-snapshot'].includes(source.kind) || !Array.isArray(source.relative)
          || source.relative.length!==2 || !source.relative.every(segment) || !Array.isArray(source.files)
          || source.files.length<1 || source.files.length>3 || source.files.some(x=>!plain(x)||!FILES.has(x.name))
          || digest(JSON.stringify(source.files))!==source.digest)fail('INVALID_DELETE_MANIFEST');
      if(source.kind==='sessions' && source.relative[1]!==leaf)fail('INVALID_DELETE_MANIFEST');
      if(source.kind==='trash' && (!/^[a-f0-9-]{36}$/.test(source.relative[0]) || source.relative[1]!=='payload'))fail('INVALID_DELETE_MANIFEST');
      if(['migration','runtime-snapshot'].includes(source.kind) && (source.relative[0]!==digest(sessionId)||source.relative[1]!=='payload'))fail('INVALID_DELETE_MANIFEST');
      return path.join(source.kind==='sessions'?sessions:source.kind==='trash'?trash:source.kind==='runtime-snapshot'?path.join(root,'.history-runtime-020-snapshot','backups'):path.join(root,'.history-v4-migration','backups'),...source.relative);
    };
    // Verify every remaining byte of every source before any additional
    // mutation; a partly completed previous unlink is the only tolerated miss.
    const work=[];
    for(const source of entry.sources) {
      const directory=sourcePath(source);
      if(source.kind==='trash') {
        const parent=path.dirname(directory);
        if(!await safeDirectory(parent,true))continue;
        const names=await readdir(parent);
        if(names.some(name=>!['manifest.json','payload'].includes(name)))fail('DELETE_CONFLICT');
        if(names.includes('manifest.json')) {
          const {manifest}=await trashManifest(source.relative[0]);
          if(manifest.sessionId!==sessionId)fail('INVALID_DELETE_MANIFEST');
        } else if(names.includes('payload'))fail('INVALID_DELETE_MANIFEST');
      }
      if(!await safeDirectory(directory,true))continue;
      const names=await readdir(directory);
      for(const name of names) {
        const expected=source.files.find(x=>x.name===name);
        if(!expected || !FILES.has(name))fail('DELETE_CONFLICT');
        const actual=await fileFingerprint(path.join(directory,name));
        if(JSON.stringify({name,...actual})!==JSON.stringify(expected))fail('SNAPSHOT_CHANGED');
      }
      work.push({directory,names});
    }
    if(entry.status==='deleted' && work.length)fail('SESSION_REAPPEARED');
    if(entry.status==='deleted')return {status:'deleted',sessionId,alreadyAbsent:true,sharedFilesRetained:true};
    await fence();
    await deleteMetadata(root,runtimeRoot,sessionId,false,16*1024*1024,entry.preserve);
    for(const {directory,names} of work) {
      await fence();for(const name of names)await unlink(path.join(directory,name));
      await rmdir(directory);await syncDirectory(path.dirname(directory));
    }
    for(const source of entry.sources.filter(x=>x.kind==='trash')) {
      const directory=path.dirname(sourcePath(source));
      if(!await safeDirectory(directory,true))continue;
      await fence();const names=await readdir(directory);
      if(names.some(name=>name!=='manifest.json'))fail('DELETE_CONFLICT');
      if(names.length)await unlink(path.join(directory,'manifest.json'));
      await rmdir(directory);await syncDirectory(trash);
    }
    await writeManifest(journal,{...entry,status:'deleted'});
    return {status:'deleted',sessionId,alreadyAbsent:entry.sources.length===0,sharedFilesRetained:true};
  }
  return {
    inventory: () => locked(async () => {const value=await deletionInventory();return {complete:value.complete,sessions:value.sessions.map(x=>({sessionId:x.sessionId,relative:x.relative,nativeFormat:x.header.version})),skipped:[]};}),
    exclusive: fn => locked(() => fn({root,sessions,fence,deletionInventory})),
    deleteSession: ({sessionId}) => locked(() => deleteSession(sessionId)),
    archivedSessions: () => locked(async () => {
      await incrementalMigrationComplete();
      const directory=path.join(root,'dsh','storages');
      // A missing ancestor is not an error for a newly-created state volume.
      if(!await safeDirectory(path.join(root,'dsh'),true))return {sessionIds:[],complete:true};
      if(!await safeDirectory(directory,true))return {sessionIds:[],complete:true};
      const file=path.join(directory,'workspace.json');
      if(!await exists(file))return {sessionIds:[],complete:true};
      await upstream(runtimeRoot);
      const {workspaceDomainSpec}=await import(pathToFileURL(path.join(runtimeRoot,'node_modules/@deepseek-ai/dsh-workspace/lib/index.js')).href);
      const document=JSON.parse((await rawFile(file,maxBytes)).bytes);
      if(document?.unit?.name!=='workspace' || document.unit.version!==2)fail('UNSUPPORTED_REGISTRY');
      const value=workspaceDomainSpec.global.schema.parse(document.global),ids=value.archivedSessionIds;
      const identity=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
      if(value.pendingMutation || ids.length>maxSessions || ids.some(id=>!identity.test(id)) || new Set(ids).size!==ids.length)
        return {sessionIds:[],complete:false};
      return {sessionIds:ids,complete:true};
    }),
  };
}

export const migrationInternals = {safeDirectory,exists,syncDirectory,rawFile,fileFingerprint,deletionSnapshot,deletionHeader,writeManifest,upstream,digest,fail,validGenerations};
