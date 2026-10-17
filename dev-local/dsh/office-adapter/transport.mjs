import fs from 'node:fs';
import path from 'node:path';
import { spawn, execFile } from 'node:child_process';

export const PROTOCOL = 'megmeet-office-v1';
export function engineConfig(config) {
  if (!config || !path.isAbsolute(config.engineRoot || '') || !path.isAbsolute(config.pythonExecutable || '')) throw Error('OFFICE_ENGINE_CONFIG_INVALID');
  const root = fs.realpathSync(config.engineRoot);
  // Python locates pyvenv.cfg from the invoked executable path. Resolving a
  // trusted venv/bin/python symlink would silently run the base interpreter.
  const python = path.resolve(config.pythonExecutable);
  fs.realpathSync(python);
  const script = path.join(root, 'engine.py');
  for (const file of [python, script]) if (!fs.statSync(file).isFile()) throw Error('OFFICE_ENGINE_CONFIG_INVALID');
  const timeoutMs = config.timeoutMs ?? 120000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 300000) throw Error('OFFICE_ENGINE_TIMEOUT_INVALID');
  return Object.freeze({ root, python, script, timeoutMs });
}

function childEnvironment(config) {
  const env = { PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONUTF8: '1' };
  // No IAM, TokenKey, proxy credentials, PYTHONPATH or NODE_OPTIONS inherited.
  for (const name of ['SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL']) if (process.env[name]) env[name] = process.env[name];
  env.PATH = process.platform === 'win32'
    ? [path.dirname(config.python), path.join(process.env.SystemRoot || 'C:\\Windows', 'System32')].join(path.delimiter)
    : [path.dirname(config.python), '/usr/bin', '/bin'].join(path.delimiter);
  return env;
}

// All sessions in this Host share one plugin module; OCR/conversion must not
// multiply the container's bounded memory by running in parallel sessions.
let engineBusy = false;
export async function runEngine(config, request, signal, spawnProcess = spawn) {
  if (engineBusy) {
    const error = Error('OFFICE_BUSY: 办公文件处理正在进行，请稍后重试。');
    error.code = 'OFFICE_BUSY';
    throw error;
  }
  engineBusy = true;
  try { return await runEngineExclusive(config, request, signal, spawnProcess); }
  finally { engineBusy = false; }
}

function runEngineExclusive(config, request, signal, spawnProcess) {
  signal?.throwIfAborted();
  const payload = JSON.stringify(request);
  if (Buffer.byteLength(payload) > 1024 * 1024) throw Error('OFFICE_REQUEST_TOO_LARGE');
  return new Promise((resolve, reject) => {
    let child, failure, stdout = [], bytes = 0, timer, killTimer, closed = false, stopping = false;
    function stop(reason) {
      failure ??= reason;
      if (!child || closed || stopping) return;
      stopping = true;
      if (process.platform === 'win32' && Number.isSafeInteger(child.pid)) {
        // Only the child this invocation owns, including its converter children.
        execFile(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'],
          { windowsHide: true, timeout: 5000 }, () => { if (!closed) child.kill(); });
      } else {
        try { process.kill(-child.pid, 'SIGTERM'); } catch { child.kill(); }
        killTimer ??= setTimeout(() => { if (!closed) { try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); } } }, 1500);
      }
    }
    const aborted = () => stop(Error('OFFICE_CANCELLED'));
    try {
      child = spawnProcess(config.python, ['-I', '-B', config.script], {
        cwd: config.root, env: childEnvironment(config), shell: false, windowsHide: true,
        detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch { reject(Error('OFFICE_ENGINE_START_FAILED')); return; }
    timer = setTimeout(() => stop(Error('OFFICE_ENGINE_TIMEOUT')), config.timeoutMs);
    signal?.addEventListener('abort', aborted, { once: true });
    if (signal?.aborted) aborted();
    child.once('error', () => { failure ??= Error('OFFICE_ENGINE_START_FAILED'); });
    child.stdin.on('error', () => { failure ??= Error('OFFICE_ENGINE_INPUT_FAILED'); });
    child.stdout.on('data', value => {
      bytes += value.length;
      if (bytes > 2 * 1024 * 1024) stop(Error('OFFICE_RESPONSE_TOO_LARGE'));
      else stdout.push(value);
    });
    // Drain converter diagnostics without leaking document text or local paths.
    child.stderr.on('data', () => {});
    child.once('close', code => {
      closed = true; clearTimeout(timer); clearTimeout(killTimer); signal?.removeEventListener('abort', aborted);
      if (failure) { reject(failure); return; }
      if (code !== 0) { reject(Error('OFFICE_ENGINE_FAILED')); return; }
      try {
        const body = JSON.parse(Buffer.concat(stdout).toString('utf8'));
        if (!body || body.protocol !== PROTOCOL || typeof body.ok !== 'boolean') throw Error('OFFICE_RESPONSE_INVALID');
        if (!body.ok) {
          const error = Error(typeof body.error?.message === 'string' ? body.error.message.slice(0, 1000) : '办公处理失败');
          error.code = typeof body.error?.code === 'string' ? body.error.code.slice(0, 100) : 'OFFICE_ENGINE_REFUSED';
          reject(error); return;
        }
        if (!Object.hasOwn(body, 'result')) throw Error('OFFICE_RESPONSE_INVALID');
        resolve(body.result);
      } catch (error) { reject(error instanceof SyntaxError ? Error('OFFICE_RESPONSE_INVALID') : error); }
    });
    child.stdin.end(payload + '\n');
  });
}
