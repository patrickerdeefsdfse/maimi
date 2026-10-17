/** Real native HTTP contract, synthetic local state, no model/company network. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, rm, writeFile, readFile } from 'node:fs/promises';
import { createServer } from 'node:net';
import { once } from 'node:events';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';
import { Script } from 'node:vm';
import { createHistoryMaintenance } from './history-native.mjs';
const runtimeRoot = process.env.HISTORY_TEST_RUNTIME || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../desktop-local/runtime');
const { unzipSync } = createRequire(path.join(runtimeRoot, 'package.json'))('fflate');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function availablePort() {
  const server = createServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const port = server.address().port; await new Promise(resolve => server.close(resolve)); return port;
}
test('native parent deletion preserves seeded subagent reading, export and fork workspace', { timeout: 180000 }, async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'megmeet-native-history-rpc-'));
  // HISTORY_TEST_WORKSPACE=/workspace is only for an isolated Docker tmpfs,
  // matching the packaged cloud guard. Never point it at a real workspace.
  const stateRoot = path.join(root, 'state'), workspace = process.env.HISTORY_TEST_WORKSPACE || path.join(root, 'project');
  const temporary=path.join(root,'tmp');
  for (const dir of [stateRoot, workspace, temporary, path.join(stateRoot, 'home'), path.join(stateRoot, 'dsh')]) await mkdir(dir, { recursive: true });
  await writeFile(path.join(workspace, 'project-kept.txt'), 'synthetic independent project data');
  let child;
  const stop = async () => {
    if (!child) return;
    if (child.exitCode !== null || child.signalCode !== null) { child = undefined; return; }
    const stopping = child, exited = once(stopping, 'exit'); stopping.kill('SIGTERM');
    const timer = setTimeout(() => { if (stopping.exitCode === null && stopping.signalCode === null) stopping.kill('SIGKILL'); }, 10000);
    try { await exited; } finally { clearTimeout(timer); }
    child = undefined;
  };
  t.after(async () => { await stop(); await rm(root, { recursive: true, force: true }); });
  const start = async () => {
    const port = await availablePort(), origin = 'http://127.0.0.1:' + port;
    let output = '', cookie = '';
    child = spawn(process.execPath, ['--expose-internals', path.join(runtimeRoot, 'node_modules/@deepseek-ai/dsh/lib/bin.js'),
      '--profile', 'web', '--host', '127.0.0.1', '--port', String(port), '--no-open'], {
      cwd: workspace, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
      env: { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot || '', HOME: path.join(stateRoot, 'home'),
        USERPROFILE: path.join(stateRoot, 'home'), DSH_HOME: path.join(stateRoot, 'dsh'),
        TEMP: temporary, TMP: temporary, NARB_DISABLE_NATIVE_CACHE: '1',
        DSH_TELEMETRY_DISABLED: '1', NODE_OPTIONS: '--max-old-space-size=512' },
    });
    child.stdout.on('data', data => { output = (output + data).slice(-65536); });
    child.stderr.on('data', data => { output = (output + data).slice(-65536); });
    for (let n = 0; n < 120; n++) {
      const token = output.match(/http:\/\/127\.0\.0\.1:\d+\/\?token=([A-Za-z0-9_-]+)/u)?.[1];
      if (token) {
        const response = await fetch(origin + '/?token=' + token, { redirect: 'manual', signal: AbortSignal.timeout(5000) });
        cookie = response.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
        if (cookie) break;
      }
      if (child.exitCode !== null) throw new Error('synthetic native process failed before ready: ' + child.exitCode);
      await delay(250);
    }
    assert.ok(cookie, 'synthetic native ready: ' + output.replace(/token=[A-Za-z0-9_-]+/gu, 'token=[redacted]').slice(-1600)); output = '';
    const headers = { Cookie: cookie, Origin: origin, 'Content-Type': 'application/json' };
    if(process.env.HISTORY_VERIFY_BRAND==='1') {
      const html=await(await fetch(origin+'/',{headers})).text();
      const urls=[...html.matchAll(/(?:src|href)=["']([^"']+)["']/g)].map(x=>x[1].replaceAll('&amp;','&'));
      const url=urls.find(x=>x.includes('dsh-client-ui-brand-official'));
      assert.ok(url,'native HTML announces the brand module batch');
      const resource=await fetch(new URL(url,origin),{headers});assert.equal(resource.status,200);
      const body=await resource.text();new Script(body);
      assert.equal(body.includes('function HistoryTrash('),false);assert.equal(body.includes('h(HistoryTrash)'),false);
      assert.ok(body.includes('30 天未访问自动清空'));assert.ok(body.includes('归档永久删除'));
      assert.ok(body.includes('data:image/'));
      console.log('NATIVE_HTTP_BRAND_MODULE_NO_TRASH_NEW_POLICY_AND_IMAGES: PASS');
    }
    return {
      async rpc(method, args) {
        const response = await fetch(origin + '/api/' + method, { method: 'POST', headers, signal: AbortSignal.timeout(15000),
          body: JSON.stringify({ type: 'client-request', rpcId: randomUUID(), method, payload: { args } }) });
        const body = await response.json(); assert.equal(response.status, 200); assert.equal(body.result?.ok, true, JSON.stringify(body));
        return body.result.value;
      },
      async export(id) {
        const response = await fetch(origin + '/api/session.export?sessionId=' + encodeURIComponent(id), { headers, signal: AbortSignal.timeout(15000) });
        if(response.status!==200)throw Error('synthetic export '+response.status+': '+(await response.text()).slice(0,3000));
        return Buffer.from(await response.arrayBuffer());
      },
    };
  };
  let native = await start();
  const registered = await native.rpc('workspace/create', { request: { path: workspace } });
  const { sessionId } = await native.rpc('session/create', { request: { workspaceId: registered.workspace.workspaceId } });
  await native.rpc('session/rename', { request: { sessionId, title: 'Synthetic archival contract' } });
  const beforeExport = await native.export(sessionId); assert.ok(beforeExport.length > 100);
  console.log('NATIVE_CREATE_RENAME_AND_DURABLE_EXPORT: PASS');
  await stop();
  const nativeBase=path.join(runtimeRoot,'node_modules','@deepseek-ai');
  const {Context}=await import(pathToFileURL(path.join(nativeBase,'cordis/lib/index.js')).href);
  const {default:Backend}=await import(pathToFileURL(path.join(nativeBase,'dsh-session-persistence-jsonl/lib/index.js')).href);
  const ctx=new Context(),fiber=ctx.plugin(Backend,{root:path.join(stateRoot,'dsh','sessions'),compression:'zstd'});
  await fiber.await();const backend=ctx.get('sessionPersistence');
  const parent=await backend.open(sessionId,'write');
  const read=await backend.open(sessionId,'read'),existing=(await read.read()).events;await read.close();
  const n=existing.length,time=Date.now();
  await parent.append([{type:'turn/start',seq:n,time,data:{turn:0}},
    {type:'turn/end',seq:n+1,time:time+1,data:{turn:0,reason:{kind:'completed'}}}]);
  await parent.flush();await parent.close();
  const source=await backend.open(sessionId,'read'),events=(await source.read()).events;
  const childId='session-'+randomUUID();
  const childHeader={...source.header,id:childId,parentSession:sessionId,origin:'subagent',isSeeded:true,delegationDepth:1,createdAt:time+2};
  await source.close();
  const seeded=await backend.create(childHeader,{inheritedEventCount:events.length});
  await seeded.append([...events,{type:'session/end-seed',seq:events.length,time:time+3,data:{inherited:true}}]);
  await seeded.flush();await seeded.close();await fiber.dispose();
  native=await start();
  const childBefore=unzipSync(await native.export(childId));
  const firstFork=await native.rpc('session/fork',{request:{sessionId:childId}});
  assert.ok(firstFork.sessionId);
  await stop();
  const maintenance=createHistoryMaintenance({stateRoot,runtimeRoot,assertQuiescent:async()=>({stopped:!child,exclusive:true})});
  const deleted=await maintenance.deleteSession({sessionId});
  assert.equal(deleted.status,'deleted',JSON.stringify(deleted));
  const state=JSON.parse(await readFile(path.join(stateRoot,'dsh','storages','workspace.json'),'utf8'));
  const registration=state.tables.workspaces[registered.workspace.workspaceId];
  assert.ok(registration.sessionIds.includes(childId));assert.ok(!registration.sessionIds.includes(sessionId));
  native=await start();
  const after=await native.rpc('session/list',{_request:{}});
  assert.ok(!after.items.some(item=>item.sessionId===sessionId));
  assert.ok(after.items.some(item=>item.sessionId===firstFork.sessionId));
  const childAfter=unzipSync(await native.export(childId));
  // Child's native log payload remains exact; descendants were added above.
  for(const [name,bytes] of Object.entries(childBefore))assert.deepEqual(childAfter[name],bytes);
  const secondFork=await native.rpc('session/fork',{request:{sessionId:childId}});
  assert.ok(secondFork.sessionId);
  const sameWorkspace=await native.rpc('workspace/create',{request:{path:workspace}});
  assert.equal(sameWorkspace.workspace.workspaceId,registered.workspace.workspaceId);
  await stop();
  const finalRegistry=JSON.parse(await readFile(path.join(stateRoot,'dsh','storages','workspace.json'),'utf8'));
  assert.ok(finalRegistry.tables.workspaces[registered.workspace.workspaceId].sessionIds.includes(secondFork.sessionId));
  assert.equal(await readFile(path.join(workspace,'project-kept.txt'),'utf8'),'synthetic independent project data');
  console.log('NATIVE_PERMANENT_PARENT_DELETE_SEEDED_CHILD_EXPORT_FORK_AND_WORKSPACE: PASS');
});
