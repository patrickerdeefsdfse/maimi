/** Synthetic native format fixtures only. Run with the pinned Node 24 runtime. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm, utimes, readdir, writeFile, appendFile, readFile, symlink, rename, cp } from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { randomBytes } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHistoryMaintenance } from './history-native.mjs';

const runtimeRoot = process.env.HISTORY_TEST_RUNTIME || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../desktop-local/runtime');
const base = path.join(runtimeRoot, 'node_modules', '@deepseek-ai');
const runtimeVersion = JSON.parse(await readFile(path.join(base, 'dsh-session/package.json'), 'utf8')).version;
const { Context } = await import(pathToFileURL(path.join(base, 'cordis/lib/index.js')).href);
const { default: Backend } = await import(pathToFileURL(path.join(base, 'dsh-session-persistence-jsonl/lib/index.js')).href);
const DAY = 86400000;
const clock = Date.UTC(2026, 8, 18, 12);

async function fixture(t, specs = [{}]) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'megmeet-synthetic-history-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const sessions = path.join(root, 'dsh', 'sessions');
  await mkdir(sessions, { recursive: true });
  const ctx = new Context();
  const fiber = ctx.plugin(Backend, { root: sessions, compression: 'zstd' });
  await fiber.await();
  const backend = ctx.get('sessionPersistence');
  const created = [];
  for (const [i, spec] of specs.entries()) {
    const time = clock - (spec.age ?? 91) * DAY;
    const id = spec.id || 'synthetic-session-' + i;
    const header = { version: 3, id, cwd: '/workspace', createdAt: time, isSeeded: false, delegationDepth: 0, ...spec.header };
    const handle = await backend.create(header);
    const events = spec.events ?? [
      { type: 'turn/start', seq: 0, time, data: { turn: 0 } },
      { type: 'turn/end', seq: 1, time: time + 1, data: { turn: 0, reason: { kind: 'completed' } } },
    ];
    await handle.append(events); await handle.flush(); await handle.close();
    const file = backend.locate(header).path;
    await utimes(file, time / 1000, time / 1000);
    created.push({ id, file, directory: path.dirname(file), header, events });
  }
  await fiber.dispose();
  let allowed = true, exclusive = true, currentTime = clock, archived = true;
  const manager = createHistoryMaintenance({ stateRoot: root, runtimeRoot,
    assertQuiescent: async () => ({ stopped: allowed, exclusive }), now: () => currentTime,
    confirmArchived: async ({ digest }) => ({ stored: archived, digest }) });
  return { root, sessions, created, manager, stopped: value => { allowed = value; }, exclusive: value => { exclusive = value; },
    clock: value => { currentTime = value; }, archived: value => { archived = value; } };
}

test('explicit permanent delete clears exact registry and projection data, retains shared files, and retries', async t => {
  if (runtimeVersion !== '0.1.6-alpha.2') return t.skip('permanent deletion targets deployed alpha.2');
  const f=await fixture(t,[{id:'delete-target',age:1},{id:'keep-target',age:1}]);
  const storages=path.join(f.root,'dsh','storages'),cache=path.join(storages,'session_projcache','sessions');
  await mkdir(cache,{recursive:true});
  const registry={unit:{name:'workspace',version:2},global:{initialized:true,workspaceIds:['workspace-test'],archivedSessionIds:['delete-target']},
    tables:{workspaces:{'workspace-test':{path:'/workspace',title:'Synthetic workspace',createdAt:new Date(clock).toISOString(),updatedAt:new Date(clock).toISOString(),sessionIds:['delete-target','keep-target']}}}};
  await writeFile(path.join(storages,'workspace.json'),JSON.stringify(registry));
  await writeFile(path.join(cache,'delete-target.json'),'synthetic projection');
  await writeFile(path.join(cache,'delete-target.json.bak.synthetic'),'synthetic old projection');
  await writeFile(path.join(cache,'keep-target.json'),'retained projection');
  await writeFile(path.join(f.root,'shared-file.txt'),'retained shared bytes');
  const result=await f.manager.deleteSession({sessionId:'delete-target'});
  assert.equal(result.status,'deleted');assert.equal(result.sharedFilesRetained,true);
  assert.deepEqual((await f.manager.inventory()).sessions.map(x=>x.sessionId),['keep-target']);
  const saved=JSON.parse(await readFile(path.join(storages,'workspace.json'),'utf8'));
  assert.deepEqual(saved.global.archivedSessionIds,[]);assert.deepEqual(saved.tables.workspaces['workspace-test'].sessionIds,['keep-target']);
  assert.deepEqual(await readdir(cache),['keep-target.json']);
  assert.equal(await readFile(path.join(f.root,'shared-file.txt'),'utf8'),'retained shared bytes');
  assert.equal((await f.manager.deleteSession({sessionId:'delete-target'})).status,'deleted');
});

test('permanent deletion refuses incomplete dependency inventory and unsafe registry before deleting log', async t => {
  if (runtimeVersion !== '0.1.6-alpha.2') return t.skip('permanent deletion targets deployed alpha.2');
  const f=await fixture(t,[{id:'delete-target'}]);
  const storages=path.join(f.root,'dsh','storages');await mkdir(storages,{recursive:true});
  await writeFile(path.join(storages,'workspace.json'),'{}');
  await assert.rejects(f.manager.deleteSession({sessionId:'delete-target'}));
  assert.ok((await readFile(f.created[0].file)).length);
  await rm(path.join(storages,'workspace.json'));
  await writeFile(path.join(f.created[0].directory,'unknown.bin'),'unrecognized');
  assert.equal((await f.manager.deleteSession({sessionId:'delete-target'})).status,'deferred');
});

test('permanent delete removes legacy trash immediately and resumes a partial multi-source deletion', async t => {
  if (runtimeVersion !== '0.1.6-alpha.2') return t.skip('permanent deletion targets deployed alpha.2');
  const f=await fixture(t,[{id:'delete-target'}]);
  const item=(await f.manager.inventory()).sessions[0];
  const old=await f.manager.quarantine({sessionId:item.sessionId,expectedDigest:item.digest});
  const ctx=new Context(),fiber=ctx.plugin(Backend,{root:f.sessions,compression:'zstd'});await fiber.await();
  const writer=await ctx.get('sessionPersistence').create(f.created[0].header);
  await writer.append(f.created[0].events);await writer.flush();await writer.close();await fiber.dispose();
  let fences=0;
  const interrupted=createHistoryMaintenance({stateRoot:f.root,runtimeRoot,assertQuiescent:async()=>({stopped:++fences!==5,exclusive:true})});
  await assert.rejects(interrupted.deleteSession({sessionId:item.sessionId}),/NATIVE_NOT_QUIESCENT/);
  await assert.rejects(readFile(f.created[0].file),{code:'ENOENT'});
  assert.ok((await readdir(path.join(f.root,'.history-trash',old.trashId,'payload'))).length);
  assert.equal((await f.manager.deleteSession({sessionId:item.sessionId})).status,'deleted');
  assert.deepEqual(await readdir(path.join(f.root,'.history-trash')),[]);
  assert.equal((await f.manager.deleteSession({sessionId:item.sessionId})).status,'deleted');
});

test('archived migration reports only validated native ids and never calls a partial inventory complete', async t => {
  if (runtimeVersion !== '0.1.6-alpha.2') return t.skip('permanent deletion targets deployed alpha.2');
  const f=await fixture(t,[]),storages=path.join(f.root,'dsh','storages');await mkdir(storages,{recursive:true});
  const id='session-12345678-1234-1234-1234-123456789012';
  const doc={unit:{name:'workspace',version:2},global:{initialized:true,workspaceIds:[],archivedSessionIds:[id]},tables:{workspaces:{}}};
  const save=()=>writeFile(path.join(storages,'workspace.json'),JSON.stringify(doc));await save();
  assert.deepEqual(await f.manager.archivedSessions(),{sessionIds:[id],complete:true});
  doc.global.archivedSessionIds.push('../unknown');await save();
  assert.deepEqual(await f.manager.archivedSessions(),{sessionIds:[],complete:false});
  doc.global.archivedSessionIds=[id,id];await save();
  assert.deepEqual(await f.manager.archivedSessions(),{sessionIds:[],complete:false});
});

test('explicit deletion streams a valid compressed log larger than old 4 MiB and ignores event vocabulary', async t => {
  if (runtimeVersion !== '0.1.6-alpha.2') return t.skip('permanent deletion targets deployed alpha.2');
  const events=Array.from({length:40000},(_,seq)=>({type:'session/title',seq,time:clock-40000+seq,
    data:{title:randomBytes(144).toString('hex'),messageSeqs:[],source:{kind:'user'}}}));
  const f=await fixture(t,[{id:'large-target',age:1,events},
    {id:'retained-opaque',events:[{type:'future/opaque-tool',seq:0,time:clock,data:{opaque:true},ignorable:true}]}]);
  const bytes=await readFile(f.created[0].file);assert.ok(bytes.length>4*1024*1024);
  const limited=createHistoryMaintenance({stateRoot:f.root,runtimeRoot,maxBytes:4*1024*1024,maxDecodedBytes:16*1024*1024,
    assertQuiescent:async()=>({stopped:true,exclusive:true})});
  assert.equal((await limited.deleteSession({sessionId:'large-target'})).status,'deleted');
  assert.ok((await readFile(f.created[1].file)).length);
  assert.equal((await limited.deleteSession({sessionId:'retained-opaque'})).status,'deleted');
});

test('real upstream zstd reader preserves structured events and complete multi-file identity; quarantine/restore is exact', async t => {
  const f = await fixture(t);
  const result = await f.manager.inventory();
  assert.deepEqual(result.skipped, []);
  const item = result.sessions[0];
  assert.equal(item.cleanupEligible, true);
  assert.equal(item.header.version, 3);
  assert.equal(item.runtimeVersion, runtimeVersion);
  assert.deepEqual(item.events, f.created[0].events);
  assert.ok(item.files.some(file => file.name === 'session.v3.jsonl.zstd'));
  assert.ok(item.files.every(file => !Object.hasOwn(file, 'bytes')));
  const workspaceFile = path.join(f.root, 'separate-project-file.txt');
  await writeFile(workspaceFile, 'project remains untouched');
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  assert.equal((await f.manager.inventory()).sessions.length, 0);
  assert.equal((await f.manager.listTrash())[0].status, 'trashed');
  await f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest });
  const restored = (await f.manager.inventory()).sessions[0];
  assert.equal(restored.digest, item.digest);
  assert.deepEqual(restored.events, item.events);
  assert.equal(restored.lastActivityAt, clock);
  await assert.rejects(f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest }), /RETENTION_NOT_DUE/);
  assert.equal(await readFile(workspaceFile, 'utf8'), 'project remains untouched');
  assert.equal((await f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest })).alreadyPresent, true);
});

test('history support matrix refuses mixed reviewed packages and unreviewed releases before loading native code', async t => {
  const f = await fixture(t);
  const fakeRuntime = path.join(f.root, 'unsupported-runtime');
  const packages = ['dsh-session', 'dsh-session-persistence-jsonl'];
  for (const name of packages) await mkdir(path.join(fakeRuntime, 'node_modules/@deepseek-ai', name), { recursive: true });
  for (const versions of [['0.1.6-alpha.1', '0.1.6-alpha.2'], ['0.1.6-alpha.3', '0.1.6-alpha.3']]) {
    for (const [i, name] of packages.entries()) await writeFile(path.join(fakeRuntime, 'node_modules/@deepseek-ai', name, 'package.json'), JSON.stringify({ name: '@deepseek-ai/' + name, version: versions[i] }));
    const manager = createHistoryMaintenance({ stateRoot: f.root, runtimeRoot: fakeRuntime,
      assertQuiescent: async () => ({ stopped: true, exclusive: true }), now: () => clock });
    await assert.rejects(manager.inventory(), /UNSUPPORTED_RUNTIME/);
    assert.ok((await readFile(f.created[0].file)).length > 0);
  }
});

test('refuses a live or nonexclusive caller and missing archive receipt', async t => {
  const f = await fixture(t);
  f.stopped(false); await assert.rejects(f.manager.inventory(), /NATIVE_NOT_QUIESCENT/);
  f.stopped(true); f.exclusive(false); await assert.rejects(f.manager.inventory(), /NATIVE_NOT_QUIESCENT/);
  f.exclusive(true); const item = (await f.manager.inventory()).sessions[0];
  f.archived(false);
  await assert.rejects(f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest }), /ARCHIVE_NOT_CONFIRMED/);
  assert.equal((await f.manager.inventory()).sessions.length, 1);
});

test('recent and unclosed turns are never automatically cleaned', async t => {
  const time = clock - 92 * DAY;
  const f = await fixture(t, [{ age: 1 }, { events: [{ type: 'turn/start', seq: 0, time, data: { turn: 0 } }] }]);
  const items = (await f.manager.inventory()).sessions;
  assert.ok(items[0].reasons.includes('RETENTION_NOT_DUE'));
  assert.ok(items[1].reasons.includes('UNFINISHED_TURN'));
  for (const item of items) await assert.rejects(f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest }));
});

test('parent and child references block both sides of retention', async t => {
  const f = await fixture(t, [{ id: 'parent' }, { id: 'child', header: { parentSession: 'parent', origin: 'subagent', delegationDepth: 1 } }]);
  const result = await f.manager.inventory();
  assert.deepEqual(result.skipped, []);
  assert.equal(result.sessions.length, 2);
  assert.ok(result.sessions.every(item => item.reasons.includes('SESSION_DEPENDENCY')));
});

test('duplicate session ids across physical project roots are never projected or cleaned', async t => {
  const f = await fixture(t, [{ id: 'duplicate', header: { cwd: '/workspace/a' } }, { id: 'unrelated' }]);
  // Native creation prevents a duplicate id. Simulate an unsafe manual merge
  // of two independently valid native stores instead of bypassing that API.
  const foreign = await fixture(t, [{ id: 'duplicate', header: { cwd: '/workspace/b' } }]);
  const copied = path.join(f.sessions, path.relative(foreign.sessions, foreign.created[0].directory));
  await mkdir(path.dirname(copied), { recursive: true });
  await cp(foreign.created[0].directory, copied, { recursive: true });
  const result = await f.manager.inventory();
  assert.equal(result.complete, false);
  assert.equal(result.skipped.filter(item => item.reason === 'DUPLICATE_SESSION_ID').length, 2);
  assert.deepEqual(result.sessions.map(item => item.sessionId), ['unrelated']);
  assert.ok(result.sessions[0].reasons.includes('INCOMPLETE_DEPENDENCY_INVENTORY'));
  for (const item of f.created) assert.ok((await readFile(item.file)).length > 0);
});

test('changed source fingerprint, symlink and unknown files are fail-closed', async t => {
  const f = await fixture(t);
  const item = (await f.manager.inventory()).sessions[0];
  await utimes(f.created[0].file, 1, 2);
  await assert.rejects(f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest }), /SNAPSHOT_CHANGED/);
  await writeFile(path.join(f.created[0].directory, 'unknown.next'), 'not part of verified native layout');
  const bad = await f.manager.inventory();
  assert.equal(bad.complete, false); assert.equal(bad.sessions.length, 0);
  assert.equal(bad.skipped[0].reason, 'UNRECOGNIZED_ARTIFACTS');
});

test('incomplete compressed tail stays byte-for-byte intact and is skipped', async t => {
  const f = await fixture(t);
  await appendFile(f.created[0].file, Buffer.from([0x28, 0xb5, 0x2f]));
  const before = await readFile(f.created[0].file);
  const result = await f.manager.inventory();
  assert.equal(result.complete, false); assert.equal(result.sessions.length, 0);
  assert.deepEqual(await readFile(f.created[0].file), before);
});

test('30-day recovery window enforced and purge never touches independent project/attachment files', async t => {
  const f = await fixture(t);
  const attachment = path.join(f.root, 'dsh', 'attachments'); await mkdir(attachment); await writeFile(path.join(attachment, 'kept'), 'attachment');
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  await assert.rejects(f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest }), /RECOVERY_WINDOW_ACTIVE/);
  f.clock(clock + 30 * DAY);
  f.archived(false); await assert.rejects(f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest }), /ARCHIVE_NOT_CONFIRMED/);
  f.archived(true);
  assert.equal((await f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest })).status, 'purged');
  assert.equal((await f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest })).alreadyPurged, true);
  assert.equal(await readFile(path.join(attachment, 'kept'), 'utf8'), 'attachment');
  await assert.rejects(f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest }), /TRASH_NOT_RESTORABLE/);
});

test('newly imported child references during the recovery window prevent permanent purge', async t => {
  const f = await fixture(t, [{ id: 'parent' }]);
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  const imported = await fixture(t, [{ id: 'child', header: { parentSession: 'parent', origin: 'subagent', delegationDepth: 1 } }]);
  const target = path.join(f.sessions, path.relative(imported.sessions, imported.created[0].directory));
  await mkdir(path.dirname(target), { recursive: true });
  await cp(imported.created[0].directory, target, { recursive: true });
  f.clock(clock + 30 * DAY);
  await assert.rejects(f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest }), /SESSION_DEPENDENCY/);
  assert.equal((await f.manager.listTrash())[0].status, 'trashed');
  await f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest });
  const result = await f.manager.inventory();
  assert.equal(result.sessions.length, 2);
  assert.ok(result.sessions.every(value => value.reasons.includes('SESSION_DEPENDENCY')));
});

test('unknown current logs during recovery cannot authorize purge of unrelated trash', async t => {
  const f = await fixture(t, [{ id: 'old' }, { id: 'unknown-new' }]);
  const item = (await f.manager.inventory()).sessions.find(value => value.sessionId === 'old');
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  await writeFile(path.join(f.created[1].directory, 'future-format.bin'), 'uninterpretable dependencies');
  f.clock(clock + 30 * DAY);
  await assert.rejects(f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest }), /INCOMPLETE_DEPENDENCY_INVENTORY/);
  assert.equal((await f.manager.listTrash())[0].status, 'trashed');
});

test('crash after prepared move can restore and no current session is overwritten', async t => {
  const f = await fixture(t);
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  const manifestFile = path.join(f.root, '.history-trash', moved.trashId, 'manifest.json');
  const manifest = JSON.parse(await readFile(manifestFile, 'utf8'));
  manifest.status = 'prepared'; await writeFile(manifestFile, JSON.stringify(manifest));
  await mkdir(f.created[0].directory);
  await assert.rejects(f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest }), /RESTORE_CONFLICT/);
  await rm(f.created[0].directory, { recursive: true });
  assert.equal((await f.manager.restore({ trashId: moved.trashId, expectedDigest: item.digest })).status, 'restored');
  await assert.rejects(f.manager.restore({ trashId: '../outside', expectedDigest: item.digest }), /INVALID_TRASH_ID/);
});

test('symlinked native project and trash source are never followed', async t => {
  const f = await fixture(t);
  const link = path.join(f.sessions, 'foreign');
  try { await symlink(f.root, link, 'junction'); } catch (error) { if (error.code === 'EPERM') { t.skip('symlink creation unavailable'); return; } throw error; }
  const inv = await f.manager.inventory();
  assert.equal(inv.complete, false);
  assert.ok(inv.sessions.every(item => !item.cleanupEligible));
  await assert.rejects(f.manager.quarantine({ sessionId: inv.sessions[0].sessionId, expectedDigest: inv.sessions[0].digest }), /INCOMPLETE_DEPENDENCY_INVENTORY/);
});

test('partially completed purge resumes only verified remaining files', async t => {
  const f = await fixture(t);
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  const dir = path.join(f.root, '.history-trash', moved.trashId);
  const manifest = JSON.parse(await readFile(path.join(dir, 'manifest.json'), 'utf8'));
  manifest.status = 'purging';
  await writeFile(path.join(dir, 'manifest.json'), JSON.stringify(manifest));
  await rm(path.join(dir, 'payload', 'session.v3.jsonl.zstd'));
  f.clock(clock + 30 * DAY);
  assert.equal((await f.manager.purge({ trashId: moved.trashId, expectedDigest: item.digest })).status, 'purged');
  assert.equal((await f.manager.listTrash())[0].status, 'purged');
});

test('receipt reconciliation finishes an exact interrupted restore and retains grace', async t => {
  const f = await fixture(t);
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  const dir = path.join(f.root, '.history-trash', moved.trashId), file = path.join(dir, 'manifest.json');
  const manifest = JSON.parse(await readFile(file, 'utf8'));
  manifest.status = 'restoring'; manifest.restoringAt = clock;
  await writeFile(file, JSON.stringify(manifest));
  await rename(path.join(dir, 'payload'), f.created[0].directory);
  const [entry] = await f.manager.listTrash();
  assert.equal(entry.status, 'restored'); assert.equal(entry.restoredAt, clock); assert.equal(entry.payloadPresent, false);
  assert.equal((await f.manager.inventory()).sessions[0].lastActivityAt, clock);
});

test('receipt reconciliation never claims a changed or redirected target was restored', async t => {
  const f = await fixture(t);
  const item = (await f.manager.inventory()).sessions[0];
  const moved = await f.manager.quarantine({ sessionId: item.sessionId, expectedDigest: item.digest });
  const dir = path.join(f.root, '.history-trash', moved.trashId), file = path.join(dir, 'manifest.json');
  const manifest = JSON.parse(await readFile(file, 'utf8'));
  manifest.status = 'restoring'; manifest.restoringAt = clock;
  await writeFile(file, JSON.stringify(manifest));
  await rename(path.join(dir, 'payload'), f.created[0].directory);
  await utimes(f.created[0].file, 4, 4);
  assert.equal((await f.manager.listTrash())[0].skipped, 'SNAPSHOT_CHANGED');
  assert.equal(JSON.parse(await readFile(file, 'utf8')).status, 'restoring');
});

test('limits return explicit skip and cannot authorize deletion from a partial inventory', async t => {
  const f = await fixture(t, [{}, {}]);
  const bounded = createHistoryMaintenance({ stateRoot: f.root, runtimeRoot,
    assertQuiescent: async () => ({ stopped: true, exclusive: true }), now: () => clock,
    maxTotalEvents: 2 });
  const result = await bounded.inventory();
  assert.equal(result.complete, false);
  assert.equal(result.skipped.at(-1).reason, 'TOTAL_LOG_LIMIT');
  assert.ok(result.sessions.every(item => !item.cleanupEligible));
});
