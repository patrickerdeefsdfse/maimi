'use strict';
// Development only: this fixed local repository launcher never accepts URLs,
// shell fragments, credentials or company hosts from the renderer.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { GATEWAY } = require('./auth.cjs');
const { DEVELOPMENT } = require('./deployment.cjs');
const { diagnosticLog } = require('./diagnostic-log.cjs');
function launcherPath() {
  let current = __dirname;
  for (let n = 0; n < 8; n++, current = path.dirname(current)) {
    const candidate = path.join(current, 'ops', 'local-env', 'dev.ps1');
    if (path.basename(current) === 'new-system' && fs.existsSync(candidate)) return candidate;
  }
  throw Error('Local development Gateway launcher not found. Keep this development app inside its repository.');
}
async function ready(signal) {
  try {
    const response = await fetch(GATEWAY + '/ready', { redirect: 'error',
      signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(2000)]) : AbortSignal.timeout(2000) });
    const value = await response.json();
    return response.ok && value.status === 'ready' && value.database === 'ready' && value.redis === 'ready';
  } catch { return false; }
}
async function ensureLocalGateway({ log, signal } = {}) {
  log = diagnosticLog(log);
  signal?.throwIfAborted();
  // A ready Docker endpoint alone does not keep WSL alive after CLI clients
  // exit. Establish the owned idle anchor even on a warm development start.
  if (DEVELOPMENT) {
    await new Promise((resolve, reject) => {
      const anchor = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', launcherPath(), 'keepalive'], {
        windowsHide: true, shell: false, stdio: 'ignore',
      });
      anchor.once('error', () => reject(Error('本地开发环境保活启动失败。')));
      anchor.once('exit', code => code === 0 ? resolve() : reject(Error('本地开发环境保活启动失败。')));
    });
  }
  signal?.throwIfAborted();
  if (await ready(signal)) return;
  signal?.throwIfAborted();
  if (!DEVELOPMENT) throw Error('公司服务暂不可用，请稍后重试。');
  const launcher = launcherPath();
  log?.write('LOCAL_GATEWAY_COLD_START: starting fixed MegmeetDev development stack\n');
  const process = spawn('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', launcher, 'start'], {
    windowsHide: true, shell: false, stdio: 'ignore',
  });
  // Canceling login must not stop a stack shared by other development clients.
  let result;
  process.once('error', () => { result = 1; });
  process.once('exit', code => { result = code ?? 1; });
  for (let n = 0; n < 180; n++) {
    signal?.throwIfAborted();
    if (await ready(signal)) { log?.write('LOCAL_GATEWAY_COLD_START: ready\n'); return; }
    if (result !== undefined && result !== 0) throw Error('Local development Gateway startup failed');
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw Error('Local development Gateway startup timed out');
}
module.exports = { ensureLocalGateway, launcherPath, ready };
