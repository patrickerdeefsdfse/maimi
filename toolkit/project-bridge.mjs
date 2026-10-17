import { randomBytes, randomUUID } from 'node:crypto';
import { setTimeout as delay } from 'node:timers/promises';

export const PROJECT_KEYS = new Set(['meeting', 'employee-cards', 'infrastructure-monitor', 'heatcost', 'service']);
const SID = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const LEASE = /^mcl_[A-Za-z0-9_-]{43}$/;
const fail = (code, status = 403) => { throw Object.assign(Error(code), { code, status }); };
const plain = x => x && typeof x === 'object' && !Array.isArray(x);
const secureForms = Object.freeze({
  'monitor-login': Object.freeze({ connector: 'infrastructure-monitor', action: 'secure_login', fields: ['location','target','username','password'] }),
  'service-login': Object.freeze({ connector: 'service', action: 'secure_connect', fields: ['host','port','username','password'] }),
});
function secureForm(name) {
  if (!Object.hasOwn(secureForms, name)) fail('PROJECT_FORM_INVALID', 400);
  return secureForms[name];
}
function clearPassword(body) { if (plain(body?.fields)) body.fields.password = ''; }
const forbidden = new Set(['owner', 'owner_key', 'user_no', 'scope', 'location', 'session_id', 'native_session_id', 'origin', 'token', 'password', 'cookie', 'path', 'endpoint']);
export function projectArguments(value, depth = 0) {
  if (depth > 20) fail('PROJECT_ARGUMENTS_INVALID', 400);
  if (plain(value)) for (const [key, child] of Object.entries(value)) {
    if (forbidden.has(key.toLowerCase())) fail('PROJECT_IDENTITY_ARGUMENT_FORBIDDEN', 400);
    projectArguments(child, depth + 1);
  }
  else if (Array.isArray(value)) for (const child of value) projectArguments(child, depth + 1);
  return value;
}

export async function boundedResponse(response, maximum = 4 * 1024 * 1024) {
  const declared = response.headers.get('content-length');
  if (declared && (!/^\d+$/.test(declared) || Number(declared) > maximum)) fail('PROJECT_RESPONSE_TOO_LARGE', 502);
  const reader = response.body?.getReader(); if (!reader) fail('PROJECT_RESPONSE_INVALID', 502);
  let size = 0; const chunks = [];
  try {
    for (;;) { const { value, done } = await reader.read(); if (done) break; size += value.byteLength; if (size > maximum) fail('PROJECT_RESPONSE_TOO_LARGE', 502); chunks.push(Buffer.from(value)); }
    return JSON.parse(Buffer.concat(chunks, size).toString('utf8'));
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  finally { reader.releaseLock(); }
}

// Private employee-worker capability registry. No permit is returned to a model
// or browser. A gateway callback must present its route token AND the same lease.
export function createProjectBridge({ remote, native, authorize, current, selection, enter = () => () => {}, now = Date.now, sleep = (ms, signal) => delay(ms, undefined, { signal }) }) {
  const permits = new Map(), jobs = new Map(); let sequence = 0, closed = false;
  function scope(session, context) {
    if (closed || !SID.test(session || '') || !LEASE.test(context?.clientKey || '')) fail('PROJECT_CONTEXT_REQUIRED');
    return context.clientKey + ':' + session;
  }
  function issue(session, context, source, connector = null, action = null, lifetime = 30_000) {
    const key = scope(session, context), selected = selection(session, context);
    if (!selected || !Number.isSafeInteger(selected.revision)) fail('PROJECT_SELECTION_INVALID');
    if (source !== 'read' && (!PROJECT_KEYS.has(connector) || !selected.ids.includes(connector))) fail('PROJECT_NOT_SELECTED');
    const task = source === 'model' ? current(session) : null;
    if (source === 'model' && (!task || task.revoked || task.lease !== context.clientKey || task.sessionId !== session)) fail('PROJECT_TASK_REQUIRED');
    if (permits.size >= 1024) fail('PROJECT_PERMIT_LIMIT', 429);
    const permit = { id: 'pi_' + randomBytes(32).toString('base64url'), key, session, lease: context.clientKey, source, connector, action,
      revision: selected.revision, generation: task?.generation ?? ++sequence, task, deadline: now() + lifetime, revoked: false,
      controller: new AbortController(), job: null };
    permits.set(permit.id, permit); return permit;
  }
  async function verify(permit, { allowRevoked = false } = {}) {
    if (!permit || closed || (!allowRevoked && permit.revoked) || permit.deadline <= now()) fail('PROJECT_PERMIT_REVOKED');
    await authorize({ clientKey: permit.lease });
    if (!allowRevoked) {
      const selected = selection(permit.session, { clientKey: permit.lease });
      if (permit.controller.signal.aborted || !selected || selected.revision !== permit.revision
          || (permit.source !== 'read' && !selected.ids.includes(permit.connector))) fail('PROJECT_SELECTION_CHANGED');
      if (permit.source === 'model' && (current(permit.session) !== permit.task || permit.task.revoked || permit.task.generation !== permit.generation)) fail('PROJECT_TASK_REVOKED');
    }
    if (permit.deadline <= now() || closed || (!allowRevoked && permit.revoked)) fail('PROJECT_PERMIT_REVOKED');
    return permit;
  }
  const envelope = (permit, value = {}) => ({ ...value, native_session_id: permit.session, invocation_id: permit.id });
  async function call(operation, permit, value, signal = permit.controller.signal) {
    return remote(operation, envelope(permit, value), { clientKey: permit.lease, signal });
  }
  function invalidate(permit) { permit.revoked = true; permit.controller.abort(); }
  async function cancel(permit) {
    invalidate(permit);
    if (!permit.submitted) return;
    // A 'cancelling' response or failed network request is not a joined stop.
    // Keep the idle hold until a terminal ACK or the absolute job deadline.
    if (!permit.cancelling) permit.cancelling = (async () => {
      while (now() < permit.deadline) {
        try {
          const value = await call('cancel', permit, permit.job ? { job_id: permit.job } : {}, AbortSignal.timeout(8000));
          if (['cancelled', 'completed', 'failed', 'interrupted'].includes(value?.status)) return value;
        } catch { /* The revoked permit prevents further I/O and publication. */ }
        await sleep(Math.min(1000, Math.max(1, permit.deadline - now())), new AbortController().signal);
      }
      return { status: 'cancelling', job_id: permit.job };
    })();
    return permit.cancelling;
  }
  async function read(operation, session, context, body = {}) {
    if (!['catalog', 'results', 'attachments', 'current-step', 'drawing-pages', 'status'].includes(operation)) fail('PROJECT_READ_INVALID');
    const permit = issue(session, context, 'read');
    try { await verify(permit); const result = await call(operation, permit, body, context.signal); await verify(permit); return result; }
    finally { invalidate(permit); permits.delete(permit.id); }
  }
  async function humanProof(session, context) {
    scope(session, context);
    if (!/^[A-Za-z0-9_-]{43}$/.test(context.humanProof || '') || !/^[a-f0-9]{64}$/.test(context.bodySha256 || '')
        || !/^\/__megmeet\/tools\/(?:invoke|cancel-job|form-ticket|secure-form)$/.test(context.controlPath || '')) fail('PROJECT_HUMAN_REQUIRED');
    const result = await remote('human-proof', { proof: context.humanProof, control_path: context.controlPath,
      body_sha256: context.bodySha256, native_session_id: session }, context);
    if (result?.verified !== true) fail('PROJECT_HUMAN_REQUIRED');
  }
  async function start(body, context, { human = false, secure = false, background = false } = {}) {
    const session = body.session_id; scope(session, context);
    if (human) await humanProof(session, context);
    const form = secure ? (body.form ?? 'monitor-login') : null;
    const secureRoute = secure ? secureForm(form) : null;
    const connector = secureRoute?.connector ?? body.connector, action = secureRoute?.action ?? body.action;
    if (!PROJECT_KEYS.has(connector) || typeof action !== 'string') fail('PROJECT_ACTION_INVALID', 400);
    if (secure && (!human || !plain(body.fields) || Object.keys(body.fields).sort().join(',') !== [...secureRoute.fields].sort().join(',')
        || Object.values(body.fields).some(value => typeof value !== 'string'))) fail('PROJECT_FORM_INVALID', 400);
    if (!secure) { if (!plain(body.arguments || {}) || JSON.stringify(body.arguments || {}).length > 24000) fail('PROJECT_ARGUMENTS_INVALID', 400); projectArguments(body.arguments || {}); }
    if (body.request_id != null && !UUID.test(body.request_id)) fail('PROJECT_REQUEST_ID_INVALID', 400);
    if (body.parent_job_id != null && (!human || !UUID.test(body.parent_job_id))) fail('PROJECT_PARENT_INVALID', 400);
    const permit = issue(session, context, human ? 'human' : 'model', connector, action, 660_000);
    const finish = enter({ work: true }); let detach = () => {};
    try {
      if (context.signal) { const abort = () => { void cancel(permit); }; context.signal.addEventListener('abort', abort, { once: true }); detach = () => context.signal.removeEventListener('abort', abort); if (context.signal.aborted) abort(); }
      await verify(permit);
      const request = secure ? { form, ticket: body.ticket, fields: body.fields, request_id: body.request_id || randomUUID() }
        : { connector, action, arguments: body.arguments || {}, request_id: body.request_id || randomUUID() };
      if (human && body.parent_job_id != null) request.parent_job_id = body.parent_job_id;
      let value;
      try { permit.submitted = true; value = await call(secure ? 'secure-form' : 'invoke', permit, request); }
      finally { if (secure && body.fields) { body.fields.password = ''; request.fields = null; } }
      if (typeof value?.job_id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(value.job_id)) fail('PROJECT_JOB_INVALID', 502);
      permit.job = value.job_id; jobs.set(permit.job, permit);
      const completion = (async () => {
        try {
          for (;;) {
            await verify(permit);
            if (value.status === 'completed') return { ...value.result, job_id: permit.job };
            if (connector === 'service' && value.remote_status_unknown === true) fail('project_service_stop_unknown', 409);
            if (['failed', 'cancelled', 'interrupted'].includes(value.status)) fail('PROJECT_JOB_' + value.status.toUpperCase(), 409);
            if (value.status !== 'running') fail('PROJECT_JOB_INVALID', 502);
            await sleep(Math.min(2000, Math.max(250, Number(value.poll_after_ms) || 1000)), permit.controller.signal);
            await verify(permit); value = await call('status', permit, { job_id: permit.job });
          }
        } catch (error) { await cancel(permit); throw error; }
        finally { detach(); invalidate(permit); jobs.delete(permit.job); permits.delete(permit.id); finish(); }
      })();
      permit.completion = completion;
      if (background) { completion.catch(() => {}); return { status: 'running', job_id: permit.job, poll_after_ms: 1000 }; }
      return await completion;
    } catch (error) {
      if (!permit.completion) { detach(); await cancel(permit); permits.delete(permit.id); finish(); }
      throw error;
    }
  }
  async function control(action, body, context) {
    if (action === 'invoke') return start(body, context, { human: true, background: true });
    if (action === 'secure-form') {
      try { return await start(body, context, { human: true, secure: true, background: true }); }
      finally { clearPassword(body); }
    }
    if (action === 'form-ticket') {
      await humanProof(body.session_id, context);
      const route = secureForm(body.form);
      const permit = issue(body.session_id, context, 'human', route.connector, route.action);
      try { await verify(permit); const result = await call('form-ticket', permit, { form: body.form }); await verify(permit); return result; }
      finally { invalidate(permit); permits.delete(permit.id); }
    }
    if (action === 'cancel-job') {
      await humanProof(body.session_id, context); const permit = jobs.get(body.job_id);
      if (!permit || permit.key !== scope(body.session_id, context)) fail('PROJECT_JOB_NOT_FOUND', 404);
      const result=await cancel(permit); await permit.completion?.catch(() => {}); return result || { status: 'cancelled', job_id: body.job_id };
    }
    fail('PROJECT_CONTROL_UNKNOWN', 404);
  }
  async function callback(kind, body, context) {
    if (!plain(body) || !SID.test(body.native_session_id || '')) fail('PROJECT_CONTEXT_REQUIRED');
    if (kind === 'read-context') {
      if (Object.keys(body).some(key => key !== 'native_session_id')) fail('PROJECT_CONTEXT_REQUIRED');
      const permit = issue(body.native_session_id, context, 'read');
      try {
        await verify(permit);
        const timer = setTimeout(() => { invalidate(permit); permits.delete(permit.id); }, 30_000); timer.unref?.();
        return callback('context', envelope(permit, { operation: 'authorize' }), context);
      } catch (error) { invalidate(permit); permits.delete(permit.id); throw error; }
    }
    const permit = permits.get(body.invocation_id);
    if (!permit || permit.session !== body.native_session_id || permit.lease !== context.clientKey) fail('PROJECT_CONTEXT_REQUIRED');
    await verify(permit, { allowRevoked: kind === 'context' && ['cancel', 'status'].includes(body.operation) });
    if (kind === 'context') return { native_session_id: permit.session, invocation_id: permit.id, selected: selection(permit.session, context).ids,
      source: permit.source, connector: permit.connector, action: permit.action, deadline: Math.floor(permit.deadline / 1000),
      selection_revision: permit.revision, task_generation: permit.generation, revoked: permit.revoked };
    if (kind !== 'files' || !['list', 'current-step', 'read', 'read-generated', 'read-card-print', 'save', 'save-card-print'].includes(body.operation) || (permit.source === 'read' && ['save','save-card-print'].includes(body.operation))) fail('PROJECT_FILES_DENIED');
    if (body.operation === 'save-card-print' && (permit.connector !== 'employee-cards' || !['import_workbook','generate','retry_photos','edit_employee'].includes(permit.action) || body.name !== '员工工卡.docx')) fail('PROJECT_CARD_PRINT_DENIED');
    const value = { operation: body.operation, session_id: permit.session };
    if (['save','save-card-print'].includes(body.operation)) Object.assign(value, { name: body.name, data_base64: body.data_base64 });
    else if (!['list', 'current-step'].includes(body.operation)) Object.assign(value, { ref: body.ref, kind: body.kind, maximum: body.maximum });
    await verify(permit); const result = await native(value, permit.controller.signal); await verify(permit); return result;
  }
  async function cardPrint(body, context, transfer) {
    if (!plain(body) || !SID.test(body.native_session_id || '')) fail('PROJECT_CONTEXT_REQUIRED');
    const permit = permits.get(body.invocation_id);
    if (!permit || permit.session !== body.native_session_id || permit.lease !== context.clientKey) fail('PROJECT_CONTEXT_REQUIRED');
    const saving = body.operation === 'save-card-print';
    if (!saving && body.operation !== 'read-card-print') fail('PROJECT_FILES_DENIED');
    if (saving && (permit.source === 'read' || permit.connector !== 'employee-cards'
        || !['import_workbook','generate','retry_photos','edit_employee'].includes(permit.action)
        || body.name !== '员工工卡.docx')) fail('PROJECT_CARD_PRINT_DENIED');
    const fields = saving ? ['native_session_id','invocation_id','operation','name','bytes','sha256']
      : ['native_session_id','invocation_id','operation','kind','ref','maximum'];
    if (Object.keys(body).some(key => !fields.includes(key))) fail('PROJECT_FILES_DENIED');
    const metadata = saving ? {operation:body.operation,session_id:permit.session,name:body.name,bytes:body.bytes,sha256:body.sha256}
      : {operation:body.operation,session_id:permit.session,kind:body.kind,ref:body.ref,maximum:body.maximum};
    const signal = AbortSignal.any([permit.controller.signal, context.signal].filter(Boolean));
    await verify(permit); signal.throwIfAborted();
    // transfer must keep the response open until this final permit check passes.
    const result = await transfer(metadata, signal);
    await verify(permit); signal.throwIfAborted();
    return result;
  }
  async function revokeScope(session, context) {
    const key = scope(session, context), list = [...permits.values()].filter(p => p.key === key);
    for (const permit of list) invalidate(permit);
    await Promise.all(list.map(async permit => { await cancel(permit); await permit.completion?.catch(() => {}); }));
  }
  async function revokeTask(task) {
    const list = [...permits.values()].filter(p => p.source === 'model' && p.task === task);
    for (const permit of list) invalidate(permit);
    await Promise.all(list.map(async permit => { await cancel(permit); await permit.completion?.catch(() => {}); }));
  }
  async function revokeAll() { const list = [...permits.values()]; for (const permit of list) invalidate(permit); await Promise.all(list.map(async p => { await cancel(p); await p.completion?.catch(() => {}); })); permits.clear(); }
  async function close() { await revokeAll(); closed = true; }
  return { read, start, control, callback, cardPrint, revokeScope, revokeTask, revokeAll, close, get busy() { return jobs.size > 0; } };
}
