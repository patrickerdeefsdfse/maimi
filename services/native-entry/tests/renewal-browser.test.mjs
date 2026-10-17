/** Synthetic browser scheduling tests; no Cordis registration or deployment. */
import assert from 'node:assert/strict';
import test from 'node:test';
import { createRenewalBrowser } from '../candidates/renewal-browser.mjs';

const IDENTITY = '/megmeet-ai-native/__megmeet/identity';
const REFRESH = '/v1/auth/refresh-token';
const expected = Object.freeze({ user_id: '11111111-1111-4111-8111-111111111111', workspace_id: 'workspace-a' });
const response = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
const healthy = () => response({ authenticated: true, ...expected });
const required = () => response({ detail: 'authentication required' }, 403);
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const flush = async () => { for (let n = 0; n < 30; n += 1) await Promise.resolve(); };

class Events {
  listeners = new Map();
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  emit(type, value = {}) { for (const listener of [...this.listeners.get(type) || []]) listener(value); }
  count() { return [...this.listeners.values()].reduce((n, listeners) => n + listeners.size, 0); }
}

function clock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    now: () => now,
    set: (callback, delay) => { const id = ++serial; timers.set(id, { due: now + delay, callback }); return id; },
    clear: id => timers.delete(id),
    count: () => timers.size,
    advance: async ms => {
      now += ms;
      for (let attempts = 0; attempts < 100; attempts += 1) {
        const due = [...timers.entries()].filter(([, item]) => item.due <= now);
        if (!due.length) { await flush(); if (![...timers.values()].some(item => item.due <= now)) return; }
        for (const [id, item] of due) { timers.delete(id); item.callback(); }
        await flush();
      }
      throw new Error('Scheduler did not quiesce');
    },
  };
}

function environment() {
  const time = clock(), channels = new Set(), messages = [], lockCalls = [];
  let lockTail = Promise.resolve();
  const locks = {
    request(name, options, work) {
      lockCalls.push({ name, options });
      const task = lockTail.catch(() => {}).then(async () => {
        if (options.signal.aborted) throw new Error('Aborted lock');
        return work({ name, mode: options.mode });
      });
      lockTail = task;
      return task;
    },
  };
  class Channel extends Events {
    constructor(name) { super(); this.name = name; this.closed = false; channels.add(this); }
    postMessage(data) {
      if (this.closed) throw new Error('Closed channel');
      messages.push(structuredClone(data));
      for (const target of channels) {
        if (target !== this && target.name === this.name) target.emit('message', { data: structuredClone(data) });
      }
    }
    close() { this.closed = true; channels.delete(this); }
  }
  return { time, channels, messages, locks, lockCalls, Channel };
}

function page(env, fetcher, overrides = {}) {
  const browser = new Events(), document = new Events(), stateListeners = new Set();
  document.visibilityState = overrides.hidden ? 'hidden' : 'visible';
  const calls = [], states = [], identities = [], connections = [];
  let snapshot, rpcTouched = false;
  Object.assign(browser, {
    document, navigator: { locks: env.locks, onLine: true }, BroadcastChannel: env.Channel,
    performance: { now: env.time.now }, setTimeout: env.time.set, clearTimeout: env.time.clear,
    fetch: async (path, options) => { calls.push({ path, options }); return fetcher(path, options); },
  }, overrides.browser);
  const connection = {
    state: { getSnapshot: () => snapshot, subscribe: listener => { stateListeners.add(listener); return () => stateListeners.delete(listener); } },
    reconnect: () => { connections.push('reconnect'); if (overrides.emitOnReconnect) emit('after-reconnect'); },
    get rpc() { rpcTouched = true; throw new Error('Native RPC must never be accessed'); },
  };
  function emit(value) { snapshot = value; for (const listener of [...stateListeners]) listener(); }
  const adapter = createRenewalBrowser({
    browser, connection, expectedIdentity: expected, cooldownMs: 100,
    timeoutMs: overrides.timeoutMs || 20000,
    onState: state => states.push(state), onIdentityChanged: identity => identities.push(identity),
    ...overrides.options,
  });
  return {
    adapter, browser, document, connection, calls, states, identities, connections, emit,
    rpcTouched: () => rpcTouched, subscriptions: () => stateListeners.size,
    visible: value => { document.visibilityState = value ? 'visible' : 'hidden'; document.emit('visibilitychange'); },
    online: value => { browser.navigator.onLine = value; browser.emit(value ? 'online' : 'offline'); },
  };
}

test('initial check uses native public state hooks and never touches RPC', async () => {
  const env = environment(), p = page(env, healthy);
  assert.equal(p.calls.length, 0);
  await env.time.advance(0);
  assert.deepEqual(p.calls.map(call => call.path), [IDENTITY]);
  assert.equal(p.states.at(-1).status, 'authenticated');
  assert.equal(p.rpcTouched(), false);
  assert.equal(p.connections.length, 0);
  assert.equal(p.subscriptions(), 1);
  p.adapter.dispose();
});

test('state changes coalesce under cooldown and duplicate snapshots are ignored', async () => {
  const env = environment(), p = page(env, healthy);
  await env.time.advance(0);
  p.emit('one'); p.emit('two'); p.emit('two');
  await env.time.advance(99);
  assert.equal(p.calls.length, 1);
  await env.time.advance(1);
  assert.equal(p.calls.length, 2);
  assert.equal(p.connections.length, 0);
  p.emit('two');
  await env.time.advance(10000);
  assert.equal(p.calls.length, 2);
  p.adapter.dispose();
});

test('hidden or offline pages defer checks until a genuine wake event', async () => {
  const env = environment(), p = page(env, healthy, { hidden: true });
  await env.time.advance(0);
  assert.equal(p.calls.length, 0);
  p.visible(true);
  await env.time.advance(0);
  assert.equal(p.calls.length, 1);
  p.visible(true);
  await env.time.advance(100);
  assert.equal(p.calls.length, 1);
  p.online(false); p.emit('disconnected');
  await env.time.advance(100);
  assert.equal(p.calls.length, 1);
  p.online(true);
  await env.time.advance(0);
  assert.equal(p.calls.length, 2);
  p.adapter.dispose();
});

test('failed checks never create their own retry timer', async () => {
  const env = environment(), p = page(env, () => response({}, 503));
  await env.time.advance(0);
  assert.equal(p.states.at(-1).status, 'unavailable');
  await env.time.advance(1000000);
  assert.equal(p.calls.length, 1);
  assert.equal(env.time.count(), 0);
  p.adapter.dispose();
});

test('three consecutive failures pause state-driven checks; visible wake resumes', async () => {
  const env = environment(), p = page(env, () => response({}, 503));
  await env.time.advance(0);
  p.emit(1); await env.time.advance(100);
  p.emit(2); await env.time.advance(100);
  p.emit(3); p.emit(4); await env.time.advance(1000);
  assert.equal(p.calls.length, 3);
  p.visible(false); p.visible(true); await env.time.advance(0);
  assert.equal(p.calls.length, 4);
  p.adapter.dispose();
});

test('successful identity verification resets the consecutive failure count', async () => {
  const env = environment();
  let count = 0;
  const p = page(env, () => (++count === 3 || count === 6) ? healthy() : response({}, 503));
  await env.time.advance(0);
  for (let n = 1; n <= 5; n += 1) { p.emit(n); await env.time.advance(100); }
  assert.equal(p.calls.length, 6);
  assert.equal(p.states.at(-1).status, 'authenticated');
  p.adapter.dispose();
});

for (const capability of ['web-locks', 'broadcast-channel']) {
  test(`missing ${capability} allows identity GET and forbids refresh POST`, async () => {
    const env = environment();
    const browser = capability === 'web-locks' ? { navigator: { onLine: true } } : { BroadcastChannel: undefined };
    const p = page(env, required, { browser });
    await env.time.advance(0);
    assert.equal(p.calls.length, 1);
    assert.equal(p.calls[0].options.method, 'GET');
    assert(p.states.some(state => state.status === 'capability-unavailable' && state.capability === capability));
    assert.equal(p.states.at(-1).status, 'lock-failed');
    p.adapter.dispose();
  });
}

test('two expired pages share one refresh, verify separately, and never echo broadcasts', async () => {
  const env = environment();
  let authenticated = false, refreshes = 0;
  const fetcher = (path, options) => {
    if (path === REFRESH) { refreshes += 1; authenticated = true; return new Response('ignored token-like body', { status: 200 }); }
    assert.equal(options.method, 'GET');
    return authenticated ? healthy() : required();
  };
  const a = page(env, fetcher, { emitOnReconnect: true }), b = page(env, fetcher, { emitOnReconnect: true });
  await env.time.advance(0);
  await env.time.advance(100);
  assert.equal(refreshes, 1);
  assert.deepEqual(env.messages, [{ type: 'credentials-updated' }]);
  assert.equal(a.connections.length, 1);
  assert.equal(b.connections.length, 1);
  assert.equal(a.rpcTouched(), false);
  assert.equal(b.rpcTouched(), false);
  assert(env.lockCalls.every(call => call.options.mode === 'exclusive' && call.options.signal instanceof AbortSignal));
  const calls = a.calls.length + b.calls.length;
  await env.time.advance(1000000);
  assert.equal(a.calls.length + b.calls.length, calls);
  a.adapter.dispose(); b.adapter.dispose();
});

test('idle credentials signal obeys cooldown and requires server identity verification', async () => {
  const env = environment(), p = page(env, healthy);
  await env.time.advance(0);
  const sender = new env.Channel('megmeet-native-renewal-v1');
  sender.postMessage({ type: 'credentials-updated' });
  await env.time.advance(99);
  assert.equal(p.connections.length, 0);
  await env.time.advance(1);
  assert.equal(p.calls.length, 2);
  assert.equal(p.connections.length, 1);
  assert.equal(env.messages.length, 1);
  sender.close(); p.adapter.dispose();
});

test('credentials signal invalidates an in-flight old identity response before reconnect', async () => {
  const env = environment(), waiting = deferred();
  let reads = 0;
  const p = page(env, () => ++reads === 1 ? waiting.promise : healthy());
  await env.time.advance(0);
  const sender = new env.Channel('megmeet-native-renewal-v1');
  sender.postMessage({ type: 'credentials-updated' });
  waiting.resolve(healthy());
  await flush();
  assert.equal(reads, 2);
  assert.equal(p.connections.length, 1);
  assert.equal(env.messages.length, 1);
  sender.close(); p.adapter.dispose();
});

test('untrusted message payload cannot select an identity or force a refresh', async () => {
  const env = environment(), p = page(env, healthy);
  await env.time.advance(0);
  const sender = new env.Channel('megmeet-native-renewal-v1');
  for (const data of [null, 'credentials-updated', {}, { type: 'other' },
    { type: 'credentials-updated', user_id: 'attacker' }, { type: 'credentials-updated', token: 'ignored' }]) sender.postMessage(data);
  await env.time.advance(1000);
  assert.equal(p.calls.length, 1);
  assert.equal(p.connections.length, 0);
  sender.close(); p.adapter.dispose();
});

test('server identity change is terminal and no later events reconnect the old page', async () => {
  const env = environment();
  const p = page(env, () => response({ authenticated: true, ...expected, workspace_id: 'workspace-b' }));
  await env.time.advance(0);
  assert.equal(p.identities.length, 1);
  assert.equal(p.adapter.check(), false);
  p.emit(1); p.visible(false); p.visible(true); p.online(false); p.online(true);
  const sender = new env.Channel('megmeet-native-renewal-v1');
  sender.postMessage({ type: 'credentials-updated' });
  await env.time.advance(1000);
  assert.equal(p.calls.length, 1);
  assert.equal(p.connections.length, 0);
  sender.close(); p.adapter.dispose();
});

test('refresh denial pauses auto checks; new credential signal is still independently verified', async () => {
  const env = environment();
  let authenticated = false;
  const p = page(env, path => path === REFRESH ? new Response(null, { status: 401 }) : (authenticated ? healthy() : required()));
  await env.time.advance(0);
  assert.equal(p.states.at(-1).status, 'authentication-required');
  const count = p.calls.length;
  p.emit(1); await env.time.advance(1000);
  assert.equal(p.calls.length, count);
  authenticated = true;
  const sender = new env.Channel('megmeet-native-renewal-v1');
  sender.postMessage({ type: 'credentials-updated' });
  await env.time.advance(0);
  assert.equal(p.connections.length, 1);
  assert.equal(env.messages.length, 1);
  sender.close(); p.adapter.dispose();
});

test('late successful refresh after dispose cannot broadcast or reconnect', async () => {
  const env = environment(), waiting = deferred();
  const p = page(env, path => path === REFRESH ? waiting.promise : required());
  await env.time.advance(0);
  assert.equal(p.calls.at(-1).path, REFRESH);
  p.adapter.dispose();
  assert.equal(p.calls.at(-1).options.signal.aborted, true);
  waiting.resolve(new Response(null, { status: 200 }));
  await flush();
  assert.deepEqual(env.messages, []);
  assert.equal(p.connections.length, 0);
});

test('late successful refresh after round timeout cannot broadcast or reconnect', async () => {
  const env = environment(), waiting = deferred();
  const p = page(env, path => path === REFRESH ? waiting.promise : required(), { timeoutMs: 10 });
  await env.time.advance(0);
  await new Promise(resolve => setTimeout(resolve, 25));
  assert.equal(p.calls.at(-1).options.signal.aborted, true);
  waiting.resolve(new Response(null, { status: 200 }));
  await flush();
  assert.deepEqual(env.messages, []);
  assert.equal(p.connections.length, 0);
  p.adapter.dispose();
});

test('failed refresh never broadcasts and does not read a body', async () => {
  const env = environment();
  const p = page(env, path => path === REFRESH ? {
    status: 503, body: { cancel: async () => {} },
    text: () => { throw new Error('Refresh body must not be read'); },
    json: () => { throw new Error('Refresh body must not be read'); },
  } : required());
  await env.time.advance(0);
  assert.equal(p.states.at(-1).status, 'unavailable');
  assert.deepEqual(env.messages, []);
  p.adapter.dispose();
});

test('dispose clears timers, subscriptions, channel and browser listeners idempotently', async () => {
  const env = environment(), p = page(env, healthy);
  assert.equal(env.time.count(), 1);
  p.adapter.dispose(); p.adapter.dispose();
  assert.equal(env.time.count(), 0);
  assert.equal(p.subscriptions(), 0);
  assert.equal(p.document.count(), 0);
  assert.equal(p.browser.count(), 0);
  assert.equal(env.channels.size, 0);
  p.emit(1); p.visible(false); p.visible(true); p.online(false); p.online(true);
  assert.equal(p.adapter.check(), false);
  await env.time.advance(1000);
  assert.equal(p.calls.length, 0);
});

test('manual check is bounded by cooldown and can resume after repeated failures', async () => {
  const env = environment(), p = page(env, () => response({}, 503));
  await env.time.advance(0);
  for (let n = 1; n <= 2; n += 1) { p.emit(n); await env.time.advance(100); }
  assert.equal(p.adapter.check(), true);
  p.adapter.check(); p.adapter.check();
  await env.time.advance(99);
  assert.equal(p.calls.length, 3);
  await env.time.advance(1);
  assert.equal(p.calls.length, 4);
  p.adapter.dispose();
});

test('invalid controller options close the already-created channel without listeners', () => {
  for (const options of [{ expectedIdentity: { user_id: 'invalid', workspace_id: 'a' } }, { timeoutMs: 0 }]) {
    const env = environment();
    assert.throws(() => page(env, healthy, { options }));
    assert.equal(env.channels.size, 0);
    assert.equal(env.time.count(), 0);
  }
});

test('disposing a page queued for the exclusive lock aborts its grant and sends no refresh', async () => {
  const env = environment(), waiting = deferred();
  let authenticated = false;
  const fetcher = async path => {
    if (path === REFRESH) { await waiting.promise; authenticated = true; return new Response(null, { status: 200 }); }
    return authenticated ? healthy() : required();
  };
  const a = page(env, fetcher), b = page(env, fetcher);
  await env.time.advance(0);
  assert.equal(env.lockCalls.length, 2);
  assert.equal(b.calls.length, 1);
  b.adapter.dispose();
  assert.equal(env.lockCalls[1].options.signal.aborted, true);
  waiting.resolve();
  await flush();
  assert.equal(b.calls.length, 1);
  assert.equal(b.connections.length, 0);
  assert.equal(env.messages.length, 1);
  a.adapter.dispose();
});
