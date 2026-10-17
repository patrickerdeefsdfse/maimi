/** Synthetic local candidate tests; no browser/native adapter or deployment. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createRenewalController } from '../candidates/renewal-controller.mjs';

test('refresh lock receives the round abort signal and stops a queued grant on dispose', async () => {
  let lockSignal, grant;
  const h = setup([required()], { withRefreshLock: (work, { signal }) => {
    lockSignal = signal;
    grant = work;
    return new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(new Error('lock aborted')), { once: true });
    });
  }});
  const pending = h.controller.check();
  await tick();
  assert.equal(lockSignal.aborted, false);
  h.controller.dispose();
  assert.equal((await pending).status, 'disposed');
  assert.equal(lockSignal.aborted, true);
  await assert.rejects(grant());
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

const expected = Object.freeze({ user_id: '11111111-1111-4111-8111-111111111111', workspace_id: 'workspace-a' });
const current = { authenticated: true, ...expected };
const response = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});
const required = () => response({detail: 'authentication required'}, 403);
const healthy = () => response(current);
const refreshed = () => new Response('ignored refresh response', {status: 200});
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };

function setup(replies, changes = {}) {
  const calls = [], states = [], connections = [], identities = [];
  const options = {
    expectedIdentity: expected,
    fetcher: async (path, options) => {
      calls.push({path, options});
      const next = replies.shift();
      assert.notEqual(next, undefined, 'unexpected request');
      return typeof next === 'function' ? next() : next;
    },
    reconnect: () => { connections.push('reconnect'); },
    onIdentityChanged: value => { identities.push(value); },
    onState: value => { states.push(value.status); },
    withRefreshLock: async work => work(),
    ...changes,
  };
  return {controller: createRenewalController(options), calls, states, connections, identities, options};
}

test('requires every callback and valid expected identity', () => {
  const {options} = setup([]);
  for (const name of ['fetcher', 'reconnect', 'onIdentityChanged', 'onState', 'withRefreshLock']) {
    assert.throws(() => createRenewalController({...options, [name]: undefined}), TypeError);
  }
  assert.throws(() => createRenewalController({...options, expectedIdentity: {user_id: 'bad', workspace_id: 'a'}}));
});

test('healthy check does not refresh or reconnect', async () => {
  const h = setup([healthy()]);
  assert.deepEqual(await h.controller.check(), {status: 'authenticated'});
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

test('explicit expired identity refreshes under lock then verifies before one reconnect', async () => {
  const h = setup([required(), required(), refreshed(), healthy()]);
  assert.deepEqual(await h.controller.check(), {status: 'reconnect-requested'});
  assert.deepEqual(h.calls.map(c => c.path), ['/megmeet-ai-native/__megmeet/identity', '/megmeet-ai-native/__megmeet/identity', '/v1/auth/refresh-token', '/megmeet-ai-native/__megmeet/identity']);
  assert.equal(h.connections.length, 1);
  assert.deepEqual(h.identities, []);
  for (const {path, options} of h.calls) {
    assert.equal(options.method, path.includes('refresh-token') ? 'POST' : 'GET');
    assert.equal(options.credentials, 'include');
    assert.equal(options.mode, 'same-origin');
    assert.equal(options.redirect, 'error');
    assert.equal(Object.hasOwn(options, 'body'), false);
    assert.deepEqual(options.headers, {Accept: 'application/json'});
  }
});

test('lock recheck detects another tab refresh without another POST', async () => {
  const h = setup([required(), healthy()]);
  assert.equal((await h.controller.check()).status, 'reconnect-requested');
  assert.equal(h.connections.length, 1);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

for (const stage of ['first identity', 'locked identity', 'refresh', 'final identity']) {
  test(`503 at ${stage} never reconnects or starts another refresh`, async () => {
    const prefixes = { 'first identity': [], 'locked identity': [required()],
      refresh: [required(), required()], 'final identity': [required(), required(), refreshed()] };
    const h = setup([...prefixes[stage], response({detail: 'temporary'}, 503)]);
    assert.equal((await h.controller.check()).status, 'unavailable');
    assert.deepEqual(h.connections, []);
    assert.deepEqual(h.identities, []);
    assert.equal(h.calls.filter(c => c.options.method === 'POST').length, ['refresh', 'final identity'].includes(stage) ? 1 : 0);
  });
}

test('unknown denial and non-JSON response are not treated as expired credentials', async () => {
  for (const reply of [response({detail: 'other'}, 403), response({authenticated: 'true'}), new Response('<html>'), response([])]) {
    const h = setup([reply]);
    assert.equal((await h.controller.check()).status, 'invalid-response');
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.connections, []);
  }
});

test('not-enabled account does not enter refresh/login behavior', async () => {
  const h = setup([response({detail: 'account not enabled for personal pilot'}, 403)]);
  assert.equal((await h.controller.check()).status, 'account-denied');
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

test('refresh authentication failure or still unauthenticated identity stops', async () => {
  for (const tail of [[new Response(null, {status: 401})], [refreshed(), required()]]) {
    const h = setup([required(), required(), ...tail]);
    assert.equal((await h.controller.check()).status, 'authentication-required');
    assert.deepEqual(h.connections, []);
  }
});

test('lock denial/missing execution has no unlocked fallback', async () => {
  for (const withRefreshLock of [async () => { throw new Error('private lock failure'); }, async () => undefined]) {
    const h = setup([required()], {withRefreshLock});
    assert.equal((await h.controller.check()).status, 'lock-failed');
    assert.equal(h.calls.length, 1);
    assert.deepEqual(h.connections, []);
    assert.equal(JSON.stringify(h.states).includes('private'), false);
  }
});

test('same-page calls share the active promise', async () => {
  const wait = deferred();
  const h = setup([wait.promise]);
  const first = h.controller.check();
  assert.equal(h.controller.check(), first);
  await tick();
  assert.equal(h.calls.length, 1);
  wait.resolve(healthy());
  await first;
});

test('credentials notification during check is not swallowed and requires a fresh query', async () => {
  const wait = deferred();
  const h = setup([wait.promise, healthy()]);
  const first = h.controller.check();
  await tick();
  assert.equal(h.controller.credentialsChanged({authenticated: true, user_id: 'forged'}), first);
  assert.equal(h.controller.credentialsChanged(), first);
  wait.resolve(healthy());
  assert.equal((await first).status, 'reconnect-requested');
  assert.equal(h.calls.length, 2);
  assert.equal(h.connections.length, 1);
  assert.deepEqual(h.identities, []);
});

test('signal during authenticated state callback is verified before reconnect', async () => {
  const wait = deferred();
  const h = setup([healthy(), healthy()], {onState: async value => { if (value.status === 'authenticated') await wait.promise; }});
  const first = h.controller.check();
  await tick();
  const notified = h.controller.credentialsChanged();
  wait.resolve();
  assert.equal((await notified).status, 'reconnect-requested');
  await first;
  assert.equal(h.calls.length, 2);
  assert.equal(h.connections.length, 1);
});

test('signal received after reconnect begins starts only a later serial batch', async () => {
  const wait = deferred();
  let reconnects = 0;
  const h = setup([healthy(), healthy()], {reconnect: async () => { reconnects++; if (reconnects === 1) await wait.promise; }});
  const first = h.controller.credentialsChanged();
  await tick();
  const second = h.controller.credentialsChanged();
  assert.notEqual(first, second);
  assert.equal(reconnects, 1);
  wait.resolve();
  await Promise.all([first, second]);
  assert.equal(reconnects, 2);
  assert.equal(h.calls.length, 2);
});

for (const change of [{user_id: '22222222-2222-4222-8222-222222222222'}, {workspace_id: 'workspace-b'}]) {
  test(`identity change ${Object.keys(change)[0]} invalidates controller permanently`, async () => {
    const h = setup([required(), required(), refreshed(), response({...current, ...change})]);
    assert.equal((await h.controller.check()).status, 'identity-changed');
    assert.equal(h.identities.length, 1);
    assert.deepEqual(h.connections, []);
    const count = h.calls.length;
    assert.equal((await h.controller.credentialsChanged()).status, 'identity-changed');
    assert.equal((await h.controller.check()).status, 'identity-changed');
    assert.equal(h.calls.length, count);
  });
}

test('dispose during uncooperative fetch blocks late identity and reconnect callbacks', async () => {
  const wait = deferred();
  const h = setup([wait.promise]);
  const pending = h.controller.credentialsChanged();
  await tick();
  h.controller.dispose();
  assert.equal(h.calls[0].options.signal.aborted, true);
  wait.resolve(healthy());
  assert.equal((await pending).status, 'disposed');
  assert.deepEqual(h.connections, []);
  assert.deepEqual(h.identities, []);
  assert.equal((await h.controller.check()).status, 'disposed');
  assert.equal(h.calls.length, 1);
});

test('dispose while waiting for refresh lock prevents later lock work', async () => {
  const wait = deferred();
  const h = setup([required()], {withRefreshLock: async work => { await wait.promise; return work(); }});
  const pending = h.controller.check();
  await tick();
  h.controller.dispose();
  wait.resolve();
  assert.equal((await pending).status, 'disposed');
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

test('two controllers under one exclusive injected lock refresh only once', async () => {
  let fresh = false, posts = 0, tail = Promise.resolve();
  const withRefreshLock = work => {
    const job = tail.then(work);
    tail = job.catch(() => {});
    return job;
  };
  const fetcher = async (path) => {
    if (path.includes('refresh-token')) { posts++; fresh = true; return refreshed(); }
    return fresh ? healthy() : required();
  };
  const a = setup([], {fetcher, withRefreshLock}), b = setup([], {fetcher, withRefreshLock});
  const results = await Promise.all([a.controller.check(), b.controller.check()]);
  assert.deepEqual(results, [{status: 'reconnect-requested'}, {status: 'reconnect-requested'}]);
  assert.equal(posts, 1);
  assert.equal(a.connections.length, 1);
  assert.equal(b.connections.length, 1);
});

test('whole-round deadline settles an uncooperative fetch and rejects its late result', async () => {
  const wait = deferred();
  const h = setup([wait.promise], {timeoutMs: 15});
  assert.equal((await h.controller.credentialsChanged()).status, 'timeout');
  assert.equal(h.calls[0].options.signal.aborted, true);
  assert.deepEqual(h.connections, []);
  wait.resolve(healthy());
  await tick();
  assert.deepEqual(h.connections, []);
  assert.deepEqual(h.identities, []);
  assert.deepEqual(h.states, ['checking', 'timeout']);
});

test('deadline during lock wait prevents late acquisition from querying or refreshing', async () => {
  const wait = deferred();
  const h = setup([required()], {timeoutMs: 15, withRefreshLock: async work => { await wait.promise; return work(); }});
  assert.equal((await h.controller.check()).status, 'timeout');
  wait.resolve();
  await tick();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

test('dispose immediately settles even when fetch ignores abort', async () => {
  const wait = deferred();
  const h = setup([wait.promise]);
  const pending = h.controller.check();
  await tick();
  h.controller.dispose();
  assert.equal((await pending).status, 'disposed');
  wait.resolve(required());
  await tick();
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.connections, []);
});

test('finally aborts successful context too', async () => {
  const h = setup([healthy()]);
  await h.controller.check();
  assert.equal(h.calls[0].options.signal.aborted, true);
});

test('adapter returning early cannot let its delayed work refresh or reconnect', async () => {
  const wait = deferred();
  let late;
  const h = setup([required(), wait.promise], {withRefreshLock: work => { late = work(); return undefined; }});
  assert.equal((await h.controller.check()).status, 'lock-failed');
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[1].options.signal.aborted, true);
  wait.resolve(required());
  await assert.rejects(late);
  await tick();
  assert.equal(h.calls.length, 2);
  assert.deepEqual(h.connections, []);
  assert.deepEqual(h.states, ['checking', 'lock-failed']);
});

test('unknown deadline values cannot disable the bound', () => {
  const {options} = setup([]);
  for (const timeoutMs of [0, -1, Infinity, NaN, '20', 120001]) {
    assert.throws(() => createRenewalController({...options, timeoutMs}), TypeError);
  }
});

test('cookie rotation followed by identity 503 preserves reconnect intent for a later check', async () => {
  const h = setup([required(), required(), refreshed(), response({detail: 'temporary'}, 503), healthy(), healthy()]);
  assert.equal((await h.controller.check()).status, 'unavailable');
  assert.equal(h.connections.length, 0);
  assert.equal((await h.controller.check()).status, 'reconnect-requested');
  assert.equal(h.connections.length, 1);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 1);
  assert.equal((await h.controller.check()).status, 'authenticated');
  assert.equal(h.connections.length, 1);
});

test('credentials notification followed by 503 survives across rounds until verified reconnect', async () => {
  const h = setup([response({detail: 'temporary'}, 503), healthy(), healthy()]);
  assert.equal((await h.controller.credentialsChanged()).status, 'unavailable');
  assert.equal(h.connections.length, 0);
  assert.equal((await h.controller.check()).status, 'reconnect-requested');
  assert.equal(h.connections.length, 1);
  assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  assert.equal((await h.controller.check()).status, 'authenticated');
  assert.equal(h.connections.length, 1);
});

test('identity-change callback finishing after deadline cannot publish late state', async () => {
  const wait = deferred();
  const h = setup([response({...current, workspace_id: 'workspace-b'})], {
    timeoutMs: 15, onIdentityChanged: async () => { await wait.promise; },
  });
  assert.equal((await h.controller.check()).status, 'identity-changed');
  assert.deepEqual(h.states, ['checking']);
  wait.resolve();
  await tick();
  assert.deepEqual(h.states, ['checking']);
  assert.deepEqual(h.connections, []);
  assert.equal((await h.controller.check()).status, 'identity-changed');
  assert.equal(h.calls.length, 1);
});
