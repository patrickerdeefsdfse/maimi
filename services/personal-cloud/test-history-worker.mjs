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
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { createHistoryMaintenance } from './history-native.mjs';
const runtimeRoot = process.env.HISTORY_TEST_RUNTIME || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../desktop-local/runtime');
const { unzipSync } = createRequire(path.join(runtimeRoot, 'package.json'))('fflate');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function availablePort() {
  const server = createServer(); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const port = server.address().port; await new Promise(resolve => server.close(resolve)); return port;
}
test('native RPC-created session survives quarantine/restore and native registry rebuild; project file remains', { timeout: 180000 }, async t => {
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
    return {
      async rpc(method, args) {
        const response = await fetch(origin + '/api/' + method, { method: 'POST', headers, signal: AbortSignal.timeout(15000),
          body: JSON.stringify({ type: 'client-request', rpcId: randomUUID(), method, payload: { args } }) });
        const body = await response.json(); assert.equal(response.status, 200); assert.equal(body.result?.ok, true, JSON.stringify(body));
        return body.result.value;
      },
      async export(id) {
        const response = await fetch(origin + '/api/session.export?sessionId=' + encodeURIComponent(id), { headers, signal: AbortSignal.timeout(15000) });
        assert.equal(response.status, 200); return Buffer.from(await response.arrayBuffer());
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
  const now = Date.now() + 91 * 86400000;
  const maintenance = createHistoryMaintenance({ stateRoot, runtimeRoot, now: () => now,
    assertQuiescent: async () => ({ stopped: !child, exclusive: true }),
    confirmArchived: async ({ digest }) => ({ stored: true, digest }) });
  const inventory = await maintenance.inventory();
  assert.deepEqual(inventory.skipped, []);
  const snapshot = inventory.sessions.find(item => item.sessionId === sessionId);
  assert.ok(snapshot?.cleanupEligible, JSON.stringify(snapshot?.reasons));
  assert.ok(snapshot.events.length > 0);
  const trashed = await maintenance.quarantine({ sessionId, expectedDigest: snapshot.digest });
  native = await start();
  const hidden = await native.rpc('session/list', { _request: {} });
  assert.ok(!hidden.items.some(item => item.sessionId === sessionId));
  console.log('NATIVE_RESTART_FILTERS_QUARANTINED_SESSION: PASS');
  const sameWorkspace = await native.rpc('workspace/create', { request: { path: workspace } });
  assert.equal(sameWorkspace.workspace.workspaceId, registered.workspace.workspaceId);
  await stop();
  await maintenance.restore({ trashId: trashed.trashId, expectedDigest: snapshot.digest });
  native = await start();
  const restored = await native.rpc('session/list', { _request: {} });
  assert.ok(restored.items.some(item => item.sessionId === sessionId));
  const restoredWorkspace = await native.rpc('workspace/create', { request: { path: workspace } });
  assert.equal(restoredWorkspace.workspace.workspaceId, registered.workspace.workspaceId);
  // ZIP headers stamp download time; compare the actual exported artifacts.
  assert.deepEqual(unzipSync(await native.export(sessionId)), unzipSync(beforeExport));
  await stop();
  assert.equal(await readFile(path.join(workspace, 'project-kept.txt'), 'utf8'), 'synthetic independent project data');
  console.log('REAL_NATIVE_RPC_ARCHIVE_RESTORE_AND_WORKSPACE_REBUILD: PASS');
});
