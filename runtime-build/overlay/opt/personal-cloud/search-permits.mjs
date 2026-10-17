import { randomBytes } from 'node:crypto';

const SID = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const LEASE = /^mcl_[A-Za-z0-9_-]{43}$/;
export const TICKET = /^mst_[A-Za-z0-9_-]{43}$/;
export const deny = (code, status = 403) => { throw Object.assign(Error(code), { code, status }); };

// issue() is a private native-control call, never an HTTP/model/browser route.
// resolveTask must prove the native ALS generation + live agent ownership;
// knowing a session ID is never sufficient to issue or resurrect authority.
export function createSearchPermits({ resolveTask, authorize, selection, now = Date.now, limit = 32, onRelease = () => {} }) {
  const permits = new Map();
  async function verify(permit) {
    if (!permit || permits.get(permit.id) !== permit || permit.deadline <= now()
        || permit.controller.signal.aborted) deny('SEARCH_SCOPE_REVOKED');
    const task = await resolveTask(permit.scope);
    if (task !== permit.task || task.revoked || task.lease !== permit.lease) deny('SEARCH_SCOPE_REVOKED');
    await authorize(task);
    const selected = selection(permit.scope.rootSessionId, { clientKey: permit.lease });
    if (!selected?.ids?.includes('web-search') || selected.revision !== permit.revision) deny('SEARCH_SELECTION_CHANGED');
    if (permit.deadline <= now() || permit.controller.signal.aborted || await resolveTask(permit.scope) !== task)
      deny('SEARCH_SCOPE_REVOKED');
    return permit;
  }
  async function issue(scope) {
    if (!scope || !SID.test(scope.rootSessionId || '') || !SID.test(scope.sessionId || '')
        || !Number.isSafeInteger(scope.generation) || scope.generation < 1 || scope.purpose !== 'web-search')
      deny('SEARCH_NATIVE_SCOPE_REQUIRED');
    for (const [id, permit] of permits) if (permit.deadline <= now()) release(id);
    if (permits.size >= limit) deny('SEARCH_BUSY', 429);
    const task = await resolveTask(scope);
    if (!task || task.revoked || !LEASE.test(task.lease || '')) deny('SEARCH_NATIVE_SCOPE_REQUIRED');
    const selected = selection(scope.rootSessionId, { clientKey: task.lease });
    if (!selected?.ids?.includes('web-search') || !Number.isSafeInteger(selected.revision)) deny('SEARCH_NOT_SELECTED');
    // resolveTask is asynchronous; recheck before inserting after concurrent issues.
    if (permits.size >= limit) deny('SEARCH_BUSY', 429);
    const id = 'mst_' + randomBytes(32).toString('base64url');
    const permit = { id, scope: Object.freeze({ ...scope }), task, lease: task.lease,
      revision: selected.revision, deadline: now() + 90000, controller: new AbortController(),
      messagesUsed: false, fallbackUsed: false, lastCode: null, allowFallback: false };
    permits.set(id, permit);
    permit.timer = setTimeout(() => release(id), 90000); permit.timer.unref?.();
    try { await verify(permit); return id; } catch (error) { release(id); throw error; }
  }
  async function get(id) { if (!TICKET.test(id || '')) deny('SEARCH_SCOPE_REQUIRED'); return verify(permits.get(id)); }
  function release(id) { const permit = permits.get(id); if (permit) { clearTimeout(permit.timer); permit.controller.abort(); permits.delete(id); onRelease(permit); } }
  function revokeTask(task) { for (const [id, permit] of permits) if (permit.task === task) release(id); }
  async function callback(body, { clientKey }) {
    const permit = await get(body?.invocation_id);
    if (body.native_session_id !== permit.scope.rootSessionId || clientKey !== permit.lease
        || Object.keys(body).sort().join(',') !== 'invocation_id,native_session_id') deny('SEARCH_SCOPE_REQUIRED');
    return { native_session_id: permit.scope.rootSessionId, agent_session_id: permit.scope.sessionId,
      invocation_id: permit.id, generation: permit.scope.generation, selected: ['web-search'],
      selection_revision: permit.revision, deadline: Math.floor(permit.deadline / 1000), purpose: 'web-search' };
  }
  return { issue, get, verify, release, revokeTask, callback, get busy() { return permits.size > 0; } };
}
