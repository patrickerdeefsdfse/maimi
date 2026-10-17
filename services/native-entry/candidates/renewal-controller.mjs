/** Local candidate only: NOT wired to native DSH or deployed.
 * withRefreshLock(work, { signal }) must await work exactly once under an exclusive
 * cross-tab lock. Browser lock/event adapters and native callbacks still need
 * integration verification. Each round has a hard 20s deadline (timeoutMs may
 * override it). No periodic scheduler, token storage, navigation or RPC.
 */
const IDENTITY = '/megmeet-ai-native/__megmeet/identity';
const REFRESH = '/v1/auth/refresh-token';
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;
const WORKSPACE = /^[A-Za-z0-9_-]{1,128}$/;
const result = status => Object.freeze({ status });

class Failure extends Error {
  constructor(status) { super(status); this.status = status; }
}

function identityOf(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || typeof value.user_id !== 'string' || !UUID.test(value.user_id)
      || typeof value.workspace_id !== 'string' || !WORKSPACE.test(value.workspace_id)) {
    throw new Failure('invalid-response');
  }
  return Object.freeze({ user_id: value.user_id, workspace_id: value.workspace_id });
}

export function createRenewalController(options) {
  const { fetcher, expectedIdentity, reconnect, onIdentityChanged, onState, withRefreshLock, timeoutMs = 20000 } = options || {};
  for (const callback of [fetcher, reconnect, onIdentityChanged, onState, withRefreshLock]) {
    if (typeof callback !== 'function') throw new TypeError('Required renewal callback missing');
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) {
    throw new TypeError('Renewal timeout must be between 1 and 120000 milliseconds');
  }
  const expected = identityOf(expectedIdentity);
  let active = null, queued = null, terminal = null, notification = 0, pendingReconnect = false;

  function alive(context) {
    if (terminal || context.stopStatus || context.abort.signal.aborted) {
      throw new Failure(terminal || context.stopStatus || 'disposed');
    }
  }

  async function announce(context, status) {
    alive(context);
    try { await onState(result(status)); } catch { throw new Failure('callback-failed'); }
    alive(context);
  }

  async function request(context, path, method, parseIdentity = false) {
    alive(context);
    let response;
    try {
      response = await fetcher(path, { method, credentials: 'include', mode: 'same-origin',
        redirect: 'error', cache: 'no-store', headers: { Accept: 'application/json' },
        signal: context.abort.signal });
      alive(context);
      if (!Number.isInteger(response.status)) throw new Failure('invalid-response');
      if (!parseIdentity || response.status >= 500) {
        await response.body?.cancel();
        alive(context);
        return { status: response.status };
      }
      if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') || '')) {
        throw new Failure('invalid-response');
      }
      const text = await response.text();
      alive(context);
      if (text.length > 16384) throw new Failure('invalid-response');
      let body;
      try { body = JSON.parse(text); } catch { throw new Failure('invalid-response'); }
      if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Failure('invalid-response');
      return { status: response.status, body };
    } catch (error) {
      alive(context);
      throw error instanceof Failure ? error : new Failure('unavailable');
    }
  }

  async function inspect(context) {
    const version = notification;
    const { status, body } = await request(context, IDENTITY, 'GET', true);
    if (status >= 500) throw new Failure('unavailable');
    if ((status === 401 || status === 403) && body.detail === 'authentication required') {
      return { required: true, version };
    }
    if (status === 403 && body.detail === 'account not enabled for personal pilot') {
      throw new Failure('account-denied');
    }
    if (status !== 200 || body.authenticated !== true) throw new Failure('invalid-response');
    return { identity: identityOf(body), version };
  }

  async function verified(context) {
    let observed = await inspect(context);
    if (!observed.required) return observed;
    // Authentication recovery can outlive this round (e.g. cookies rotate but
    // the verification endpoint temporarily returns 503). Keep its reconnect
    // intent until a later verified round actually requests the new generation.
    pendingReconnect = true;
    context.wantReconnect = true;
    let calls = 0, completed = false, lockedResult;
    const marker = {};
    try {
      const work = async () => {
        if (++calls !== 1) throw new Failure('lock-failed');
        alive(context);
        // Another tab may have refreshed while we were waiting for this lock.
        lockedResult = await inspect(context);
        if (lockedResult.required) {
          await announce(context, 'refreshing');
          const refreshed = await request(context, REFRESH, 'POST');
          if (refreshed.status === 401 || refreshed.status === 403) throw new Failure('authentication-required');
          if (refreshed.status >= 500) throw new Failure('unavailable');
          if (refreshed.status < 200 || refreshed.status >= 300) throw new Failure('invalid-response');
          lockedResult = await inspect(context);
          if (lockedResult.required) throw new Failure('authentication-required');
        }
        completed = true;
        return marker;
      };
      const returned = await withRefreshLock(() => {
        const operation = work();
        // A faulty adapter may return before awaiting its work. Keep the late
        // rejection handled; the context fence prevents further requests.
        operation.catch(() => {});
        return operation;
      }, { signal: context.abort.signal });
      alive(context);
      if (calls !== 1 || !completed || returned !== marker) throw new Failure('lock-failed');
      return lockedResult;
    } catch (error) {
      alive(context);
      throw error instanceof Failure ? error : new Failure('lock-failed');
    }
  }

  async function finish(context, status) {
    context.closed = true;
    await announce(context, status);
    return result(status);
  }

  async function execute(context) {
    try {
      await announce(context, 'checking');
      for (;;) {
        const observed = await verified(context);
        alive(context);
        const current = observed.identity;
        if (current.user_id !== expected.user_id || current.workspace_id !== expected.workspace_id) {
          terminal = 'identity-changed';
          pendingReconnect = false;
          context.closed = true;
          try { await onIdentityChanged(Object.freeze({ expected, current })); }
          catch { return result(terminal === 'disposed' ? 'disposed' : 'callback-failed'); }
          if (terminal === 'disposed') return result('disposed');
          // Identity change is deliberately terminal, so alive() is not valid
          // here. Still fence a callback that finishes after the round deadline.
          if (context.stopStatus || context.abort.signal.aborted) return result(terminal);
          try { await onState(result('identity-changed')); } catch { return result('callback-failed'); }
          return result(terminal);
        }
        // A notification received during a query cannot reuse that query's
        // potentially older cookie snapshot. Coalesce and verify again first.
        if (observed.version !== notification) continue;
        if (context.wantReconnect) {
          await announce(context, 'reconnecting');
          if (observed.version !== notification) continue;
          context.committed = true;
          const requestedVersion = notification;
          try { await reconnect(); } catch { throw new Failure('callback-failed'); }
          alive(context);
          // A newer signal must survive an older round's completion.
          if (requestedVersion === notification) pendingReconnect = false;
          // Native reconnect() requests a new generation. It does not confirm
          // that a WebSocket handshake or subsequent RPC actually succeeded.
          return await finish(context, 'reconnect-requested');
        }
        await announce(context, 'authenticated');
        if (context.wantReconnect || observed.version !== notification) continue;
        context.closed = true;
        return result('authenticated');
      }
    } catch (error) {
      if (terminal) return result(terminal);
      if (context.stopStatus || context.abort.signal.aborted) return result(context.stopStatus || 'disposed');
      context.closed = true;
      const status = error instanceof Failure ? error.status : 'operation-failed';
      try { await onState(result(status)); } catch { return result('callback-failed'); }
      return result(terminal || status);
    }
  }

  function start(wantReconnect) {
    if (terminal) return Promise.resolve(result(terminal));
    if (wantReconnect) pendingReconnect = true;
    if (active) {
      if (wantReconnect) active.wantReconnect = true;
      if (queued) return queued;
      // A signal arriving after the reconnect side effect belongs to the next
      // serial batch; it must not disappear into an already committed flight.
      if (wantReconnect && (active.committed || active.closed)) {
        queued = active.promise.then(() => {
          queued = null;
          return start(true);
        });
        return queued;
      }
      return active.promise;
    }
    const context = { abort: new AbortController(), wantReconnect: wantReconnect || pendingReconnect,
      committed: false, closed: false, stopStatus: null };
    active = context;
    let interrupted;
    const cancellation = new Promise(resolve => { interrupted = resolve; });
    context.cancel = status => {
      if (context.stopStatus) return;
      context.stopStatus = status;
      context.abort.abort();
      if (status === 'timeout' && !terminal) {
        // A hung observer cannot delay the hard deadline either.
        try { Promise.resolve(onState(result('timeout'))).catch(() => {}); } catch {}
      }
      interrupted(result(terminal || status));
    };
    const deadline = setTimeout(() => context.cancel('timeout'), timeoutMs);
    context.promise = Promise.race([Promise.resolve().then(() => execute(context)), cancellation]).finally(() => {
      clearTimeout(deadline);
      context.closed = true;
      // Also fence work launched by a lock adapter that violated its contract.
      context.abort.abort();
      if (active === context) active = null;
    });
    return context.promise;
  }

  return Object.freeze({
    check: () => start(false),
    // No event payload, identity or token is accepted as authoritative input.
    credentialsChanged: () => { notification += 1; return start(true); },
    dispose: () => {
      terminal = 'disposed';
      pendingReconnect = false;
      active?.cancel('disposed');
      return result('disposed');
    },
  });
}
