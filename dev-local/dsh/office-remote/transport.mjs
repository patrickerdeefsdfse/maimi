import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export const PROTOCOL = 'megmeet-office-v1';
export const REMOTE_PROTOCOL = 'megmeet-office-remote-v1';
const INPUT_LIMIT = 64 * 1024 * 1024, OUTPUT_LIMIT = 128 * 1024 * 1024;
const OPERATIONS = new Set(['inspect', 'read', 'create', 'edit', 'convert', 'pdf', 'image', 'ocr']);
const MUTATING = new Set(['create', 'edit', 'convert', 'pdf', 'image']);
const jsonLimit = 2 * 1024 * 1024;
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const error = code => Object.assign(Error(code), { code });

export function engineConfig(config) {
  if (!config || typeof config.endpoint !== 'string' || typeof config.token !== 'string'
      || !/^[A-Za-z0-9_-]{32,128}$/.test(config.token)) throw error('OFFICE_REMOTE_CONFIG_INVALID');
  const endpoint = new URL(config.endpoint);
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || !endpoint.port
      || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
      || endpoint.pathname !== '/__megmeet/office') throw error('OFFICE_REMOTE_CONFIG_INVALID');
  const timeoutMs = config.timeoutMs ?? 240000;
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 300000) throw error('OFFICE_REMOTE_TIMEOUT_INVALID');
  return Object.freeze({ endpoint: endpoint.origin + endpoint.pathname, token: config.token, timeoutMs });
}

async function boundedJson(response) {
  const chunks = []; let count = 0;
  for await (const chunk of response.body ?? []) {
    count += chunk.length;
    if (count > jsonLimit) throw error('OFFICE_REMOTE_RESPONSE_TOO_LARGE');
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw error('OFFICE_REMOTE_RESPONSE_INVALID'); }
  if (!response.ok) {
    const code = typeof value?.detail === 'string' && /^[a-z0-9_:-]{1,100}$/i.test(value.detail)
      ? value.detail : 'OFFICE_REMOTE_UNAVAILABLE';
    throw error(code);
  }
  return value;
}

function relative(value) {
  if (typeof value !== 'string' || !value || value.length > 2048 || /[\x00-\x1f\x7f:]/.test(value)
      || path.isAbsolute(value) || path.win32.isAbsolute(value)) throw error('OFFICE_REMOTE_PATH_INVALID');
  const parts = value.replaceAll('\\', '/').split('/');
  if (parts.some(v => !v || v === '.' || v === '..' || /[ .]$/.test(v))) throw error('OFFICE_REMOTE_PATH_INVALID');
  return parts;
}

async function localPath(root, value, output = false) {
  const parts = relative(value); let current = root;
  for (let n = 0; n < parts.length; n++) {
    current = path.join(current, parts[n]);
    const info = await fs.lstat(current).catch(e => { if (e.code === 'ENOENT') return null; throw e; });
    if (output && n === parts.length - 1 && !info) return current;
    if (!info || info.isSymbolicLink() || n < parts.length - 1 && !info.isDirectory()) throw error('OFFICE_REMOTE_PATH_INVALID');
    if (n === parts.length - 1) {
      if (output) throw error('OFFICE_OUTPUT_EXISTS_USE_NEW_NAME');
      if (!info.isFile() || info.nlink !== 1 || info.size > INPUT_LIMIT) throw error('OFFICE_REMOTE_INPUT_INVALID');
    }
  }
  return current;
}

function extension(value) {
  const ext = path.extname(value).toLowerCase();
  if (!/^\.[a-z0-9]{1,12}$/.test(ext)) throw error('OFFICE_REMOTE_EXTENSION_INVALID');
  return ext;
}

function pause(ms, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const aborted = () => { clearTimeout(timer); reject(signal.reason); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', aborted); resolve(); }, ms);
    signal.addEventListener('abort', aborted, { once: true });
  });
}

async function* inputChunks(handle, size, signal) {
  let position = 0;
  while (position < size) {
    signal.throwIfAborted();
    const buffer = Buffer.allocUnsafe(Math.min(65536, size - position));
    const { bytesRead } = await handle.read(buffer, 0, buffer.length, position);
    if (!bytesRead) throw error('OFFICE_REMOTE_INPUT_CHANGED');
    position += bytesRead; yield buffer.subarray(0, bytesRead);
  }
}

// The original adapter authorizes session paths and attachments. This transport
// rechecks local files, sends task aliases only and creates a new output atomically.
export async function runEngine(config, request, callerSignal, fetcher = fetch) {
  config = engineConfig(config);
  if (request.officeSessionId !== undefined && !/^[A-Za-z0-9_-]{1,200}$/.test(request.officeSessionId)) throw error('OFFICE_REMOTE_SESSION_INVALID');
  // Private loopback context is never included in the uploaded job JSON.
  // Desktop drops this header; a cloud Host bridge binds it to its active lease.
  const contextHeaders = request.officeSessionId === undefined ? {} : { 'X-Megmeet-Office-Session': request.officeSessionId };
  const signal = AbortSignal.any([callerSignal, AbortSignal.timeout(config.timeoutMs)].filter(Boolean));
  const call = async (suffix, options = {}, useSignal = signal) => fetcher(config.endpoint + suffix, {
    ...options, redirect: 'error', signal: useSignal,
    headers: { Authorization: 'Bearer ' + config.token, ...contextHeaders, ...(options.headers || {}) },
  });
  const json = (suffix, options = {}, useSignal) => call(suffix, options, useSignal).then(boundedJson);
  const post = body => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  signal.throwIfAborted();
  if (request.protocol !== PROTOCOL) throw error('OFFICE_PROTOCOL_INVALID');
  if (request.operation === 'capabilities') return json('/capabilities');
  if (!OPERATIONS.has(request.operation) || !path.isAbsolute(request.workspace || '')) throw error('OFFICE_REMOTE_REQUEST_INVALID');
  const root = await fs.realpath(request.workspace);
  if (!(await fs.stat(root)).isDirectory()) throw error('OFFICE_REMOTE_WORKSPACE_INVALID');
  const mutating = MUTATING.has(request.operation);
  if (mutating !== (typeof request.output === 'string')) throw error('OFFICE_REMOTE_OUTPUT_INVALID');
  const outputPath = mutating ? await localPath(root, request.output, true) : null;
  const mappedOutput = outputPath ? 'output' + extension(request.output) : undefined;
  const merge = request.operation === 'pdf' && request.options?.action === 'merge';
  const sources = request.input === undefined ? [] : [request.input];
  if (merge) {
    if (!Array.isArray(request.options.inputs) || request.options.inputs.length < 1 || request.options.inputs.length > 20) throw error('OFFICE_MERGE_INPUTS_INVALID');
    sources.push(...request.options.inputs);
  }
  if (sources.length > 20 || !sources.length && !['create'].includes(request.operation)) throw error('OFFICE_REMOTE_INPUT_INVALID');
  const opened = [], uploads = []; let total = 0, jobId, tempFile;
  try {
    for (const [index, source] of sources.entries()) {
      const name = await localPath(root, source);
      const before = await fs.lstat(name);
      const handle = await fs.open(name, 'r'); opened.push(handle);
      const info = await handle.stat();
      if (!info.isFile() || info.nlink !== 1 || info.size > INPUT_LIMIT || before.dev !== info.dev || before.ino !== info.ino) throw error('OFFICE_REMOTE_INPUT_CHANGED');
      total += info.size;
      if (total > INPUT_LIMIT) throw error('OFFICE_MERGE_INPUT_TOO_LARGE');
      uploads.push({ handle, info, alias: 'input-' + index + extension(source) });
    }
    const options = { ...(request.options || {}) };
    if (merge) options.inputs = uploads.slice(request.input === undefined ? 0 : 1).map(v => v.alias);
    const remoteRequest = { operation: request.operation, options,
      ...(request.input === undefined ? {} : { input: uploads[0].alias }),
      ...(mappedOutput ? { output: mappedOutput } : {}) };
    const created = await json('/jobs', post({ protocol: REMOTE_PROTOCOL, request: remoteRequest,
      inputs: uploads.map(v => ({ path: v.alias, size_bytes: v.info.size })) }));
    if (created?.protocol !== REMOTE_PROTOCOL || !/^[a-zA-Z0-9_-]{1,128}$/.test(created.job_id || '') || created.state !== 'uploading') throw error('OFFICE_REMOTE_JOB_INVALID');
    jobId = created.job_id;
    for (const [index, item] of uploads.entries()) {
      signal.throwIfAborted();
      await json('/jobs/' + jobId + '/inputs/' + index, { method: 'PUT', duplex: 'half',
        headers: { 'Content-Type': 'application/octet-stream', 'Content-Length': String(item.info.size) },
        body: item.info.size ? inputChunks(item.handle, item.info.size, signal) : Buffer.alloc(0) });
      const after = await item.handle.stat();
      if (after.size !== item.info.size || after.mtimeMs !== item.info.mtimeMs || after.ctimeMs !== item.info.ctimeMs) throw error('OFFICE_REMOTE_INPUT_CHANGED');
    }
    await json('/jobs/' + jobId + '/run', post({}));
    let status;
    for (;;) {
      status = await json('/jobs/' + jobId);
      if (status?.protocol !== REMOTE_PROTOCOL || status.job_id !== jobId) throw error('OFFICE_REMOTE_JOB_INVALID');
      if (status.state === 'succeeded') break;
      if (status.state === 'failed' || status.state === 'cancelled') {
        const code = /^[A-Z0-9_-]{1,100}$/i.test(status.error?.code || '') ? status.error.code : 'OFFICE_REMOTE_FAILED';
        throw error(code);
      }
      if (!['queued', 'running'].includes(status.state)) throw error('OFFICE_REMOTE_JOB_INVALID');
      await pause(200, signal);
    }
    if (!Object.hasOwn(status, 'result') || !Array.isArray(status.outputs)
        || status.outputs.length !== (mutating ? 1 : 0)) throw error('OFFICE_REMOTE_OUTPUT_INVALID');
    if (mutating) {
      const item = status.outputs[0];
      if (!/^[A-Za-z0-9_-]{1,128}$/.test(item.id || '') || item.path !== mappedOutput
          || !Number.isSafeInteger(item.size_bytes) || item.size_bytes < 1 || item.size_bytes > OUTPUT_LIMIT
          || !/^[a-f0-9]{64}$/.test(item.sha256 || '')) throw error('OFFICE_REMOTE_OUTPUT_INVALID');
      await localPath(root, request.output, true);
      tempFile = path.join(path.dirname(outputPath), '.megmeet-office-' + randomUUID());
      const output = await fs.open(tempFile, 'wx', 0o600); let count = 0;
      const digest = createHash('sha256');
      try {
        const response = await call('/jobs/' + jobId + '/outputs/' + item.id);
        if (!response.ok) throw error('OFFICE_REMOTE_OUTPUT_UNAVAILABLE');
        for await (const chunk of response.body ?? []) {
          signal.throwIfAborted(); count += chunk.length;
          if (count > item.size_bytes || count > OUTPUT_LIMIT) throw error('OFFICE_REMOTE_OUTPUT_TOO_LARGE');
          digest.update(chunk); await output.writeFile(chunk);
        }
        if (count !== item.size_bytes || digest.digest('hex') !== item.sha256) throw error('OFFICE_REMOTE_OUTPUT_INTEGRITY');
        await output.sync();
      } finally { await output.close(); }
      signal.throwIfAborted(); await localPath(root, request.output, true);
      // link() refuses an existing destination; rename() would overwrite it.
      await fs.link(tempFile, outputPath); await fs.unlink(tempFile); tempFile = null;
    }
    return mutating ? { ...status.result, output: request.output } : status.result;
  } finally {
    await Promise.allSettled(opened.map(handle => handle.close()));
    if (tempFile) await fs.unlink(tempFile).catch(() => {});
    if (jobId) await json('/jobs/' + jobId, { method: 'DELETE' }, AbortSignal.timeout(3000)).catch(() => {});
  }
}

export { sha };
