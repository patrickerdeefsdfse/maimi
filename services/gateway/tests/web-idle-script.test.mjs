/** Executes the shipped browser scripts with synthetic DOM/time/network only. */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const source = readFileSync(new URL('../app/personal_cloud_web.py', import.meta.url), 'utf8');
function script(name) {
  const match = source.match(new RegExp(`async def ${name}\\(\\):[\\s\\S]*?return Response\\('''([\\s\\S]*?)'''`));
  assert.ok(match, `missing ${name}`);
  return match[1];
}
const ready = (extra = {}) => ({status: 'ready', idle_seconds: 600, busy: false, idle_for_seconds: 0, ...extra});
const reply = (body, status = 200) => new Response(JSON.stringify(body), {status});
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup({name = 'session_script', search = '', loading = false, frames = [], respond = () => reply(ready())} = {}) {
  let now = 1000000, nextTimer = 0;
  const calls = [], navigations = [], timers = new Map(), listeners = new Map();
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.attributes = {}; this.hidden = false; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    setAttribute(name, value) { this.attributes[name] = value; }
    remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
    focus() { this.focused = true; }
  }
  const body = new Element('body');
  function find(id, current = body) { if (current.id === id) return current; for (const child of current.children) { const value = find(id, child); if (value) return value; } return null; }
  const document = {body, readyState: loading ? 'loading' : 'complete', createElement: tag => new Element(tag), getElementById: find,
    querySelectorAll: selector => selector === 'iframe' ? frames : [],
    addEventListener(type, callback) { if (!listeners.has(type)) listeners.set(type, new Set()); listeners.get(type).add(callback); },
    removeEventListener(type, callback) { listeners.get(type)?.delete(callback); }};
  for (const id of ['state', 'login', 'retry']) { const element = new Element('div'); element.id = id; body.append(element); }
  const draft = new Element('textarea'); draft.id = 'existing-draft'; draft.value = 'Synthetic unsent draft'; body.append(draft);
  const location = {origin: 'https://app.test', href: `https://app.test/maimi/web/chat${search}`, search,
    assign: url => navigations.push(['assign', url]), replace: url => navigations.push(['replace', url])};
  const fetch = async (path, options = {}) => { calls.push({path, options}); return await respond(path, options, calls.length); };
  const window = {fetch};
  const context = vm.createContext({window, document, location, URL, URLSearchParams, Response, Date: {now: () => now},
    setInterval: (callback, ms) => { const id = ++nextTimer; timers.set(id, {callback, ms, due: now + ms}); return id; },
    clearInterval: id => timers.delete(id),
    clearTimeout: id => timers.delete(id),
    setTimeout: (callback, ms) => { const id = ++nextTimer; timers.set(id, {callback, ms, due: now + ms, once: true}); return id; }});
  Object.defineProperty(context, 'fetch', {get: () => window.fetch});
  vm.runInContext(script(name), context);
  async function advance(ms) {
    const until = now + ms;
    while (true) {
      const next = [...timers.entries()].filter(([, timer]) => timer.due <= until).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      const [id, timer] = next; now = timer.due;
      if (timer.once) timers.delete(id); else timer.due += timer.ms;
      await timer.callback(); await flush();
    }
    now = until; await flush();
  }
  async function event(type, trusted = true) { for (const callback of [...(listeners.get(type) || [])]) callback({type, isTrusted: trusted}); await flush(); }
  function elements(current = body) { return [current, ...current.children.flatMap(elements)]; }
  return {calls, navigations, timers, listeners, window, document, draft, advance, event, find, elements};
}

test('an untouched page sends one initial activity; polling never renews or expires locally', async () => {
  const h = setup(); await flush(); await h.advance(660000);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.calls.filter(call => !call.options.method).length, 66);
  assert.equal(h.find('megmeet-web-idle'), null);
  assert.deepEqual(h.navigations, []);
});

test('only trusted physical input renews, with 15 second throttling', async () => {
  const h = setup(); await flush();
  await h.advance(15000);
  for (const type of ['scroll', 'focus', 'visibilitychange']) await h.event(type);
  await h.event('wheel', false);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  for (const type of ['wheel', 'pointerdown', 'keydown', 'input', 'touchstart', 'touchmove']) {
    const count = h.calls.filter(call => call.options.method === 'POST').length;
    await h.event(type); await h.event(type);
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, count + 1);
    await h.advance(15000);
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, count + 2);
    await h.advance(15000);
  }
});

test('background task status does not fabricate user activity', async () => {
  const h = setup({respond: () => reply(ready({busy: true, idle_for_seconds: 900}))}); await flush(); await h.advance(610000);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  assert.equal(h.find('megmeet-web-idle'), null);
  assert.match(h.find('megmeet-web-session').children[0].textContent, /任务处理中/);
});

test('server release is quiet, stops reconnection, preserves the draft, and waits for explicit reentry', async () => {
  const h = setup({respond: (_path, options) => reply(options.method ? ready() : {status: 'released'})});
  await flush(); await h.advance(10000);
  assert.equal(h.find('megmeet-web-idle'), null); assert.equal(h.find('existing-draft'), h.draft);
  assert.equal(h.draft.value, 'Synthetic unsent draft'); assert.equal(h.timers.size, 0);
  const count = h.calls.length;
  await h.advance(120000); await h.event('wheel');
  const blocked = await h.window.fetch('/__megmeet/desktop/cloud/open', {method: 'POST'});
  assert.equal(blocked.status, 409); assert.equal(h.calls.length, count); assert.deepEqual(h.navigations, []);
  const blockedDownload = await h.window.fetch('/v1/personal-cloud/web/file?path=synthetic');
  assert.equal(blockedDownload.status, 409); assert.equal(h.calls.length, count);
  assert.equal(h.find('megmeet-web-session').children[0].textContent, '已退出云端，重新进入');
  assert.ok(h.elements().every(element => !/10分钟|连续.*分钟/.test(element.textContent || '')));
  h.find('megmeet-web-session').children[0].onclick();
  assert.deepEqual(h.navigations, [['assign', '/maimi/web/']]);
});

test('an expired session is explicit and does not automatically send the user to IAM', async () => {
  const h = setup({respond: () => reply({detail: 'expired'}, 401)}); await flush();
  assert.ok(h.find('megmeet-web-idle')); assert.equal(h.timers.size, 0); assert.deepEqual(h.navigations, []);
  h.elements().find(element => element.textContent === '重新登录').onclick();
  assert.deepEqual(h.navigations, [['assign', '/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F']]);
});

test('network errors leave the page intact and do not cause relogin or resource creation', async () => {
  const h = setup({respond: () => { throw Error('offline'); }}); await flush(); await h.advance(120000);
  assert.equal(h.find('megmeet-web-idle'), null); assert.deepEqual(h.navigations, []);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
});

test('an unrelated business conflict and an external 401 do not mark the workspace released', async () => {
  const h = setup({respond: path => path === 'https://external.test/api' ? reply({}, 401) : path === '/api/session/prompt' ? reply({detail: 'conflict'}, 409) : reply(ready())});
  await flush(); await h.window.fetch('/api/session/prompt'); await h.window.fetch('https://external.test/api');
  assert.equal(h.find('megmeet-web-idle'), null);
});

test('native workspace release errors are recognized without waiting for the next poll', async () => {
  const h = setup({respond: path => path === '/api/session/prompt' ? reply({detail: 'personal_cloud_open_required'}, 409) : reply(ready())});
  await flush(); await h.window.fetch('/api/session/prompt');
  assert.equal(h.find('megmeet-web-idle'), null); assert.equal(h.timers.size, 0);
  assert.equal(h.find('megmeet-web-session').children[0].textContent, '已退出云端，重新进入');
});

test('a 401 before DOM readiness waits for the DOM without starting activity or polling', async () => {
  const h = setup({loading: true, respond: () => reply({}, 401)});
  await h.window.fetch('/api/session/prompt'); assert.equal(h.find('megmeet-web-idle'), null);
  await h.event('DOMContentLoaded'); assert.ok(h.find('megmeet-web-idle')); assert.equal(h.calls.length, 1); assert.equal(h.timers.size, 0);
});

test('a stale activity response cannot silently reconnect an already released workspace', async () => {
  let resolve;
  const h = setup({respond: (_path, options) => options.method ? new Promise(done => { resolve = done; }) : reply({status: 'released'})});
  await h.advance(10000); assert.equal(h.find('megmeet-web-session').children[0].textContent, '已退出云端，重新进入');
  resolve(reply(ready())); await flush(); assert.equal(h.find('megmeet-web-session').children[0].textContent, '已退出云端，重新进入'); assert.equal(h.timers.size, 0);
});

test('the final physical action inside the throttle window is flushed even when the user stops', async () => {
  const h = setup(); await flush();
  await h.advance(14900); await h.event('wheel');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  await h.advance(100);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 2);
  await h.advance(650000);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 2);
  assert.equal(h.find('megmeet-web-idle'), null);
});

test('pointer movement and dragging count as real activity; automatic scrolling does not', async () => {
  const h = setup(); await flush();
  for (const type of ['pointermove', 'pointerup', 'click']) {
    await h.advance(15000);
    const before = h.calls.filter(call => call.options.method === 'POST').length;
    await h.event(type, false); await h.event('scroll');
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, before);
    await h.event(type);
    assert.equal(h.calls.filter(call => call.options.method === 'POST').length, before + 1);
  }
});

test('an action while the previous activity request is pending is not lost', async () => {
  let resolve;
  const h = setup({respond: (_path, options, count) => options.method && count === 1 ? new Promise(done => {resolve = done;}) : reply(ready())});
  await h.advance(16000); await h.event('input');
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  resolve(reply(ready())); await flush(); await flush();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 2);
});

test('same-origin frame input is observed, while opaque frames and frame loads do not renew activity', async () => {
  const frameListeners = new Map(), childListeners = new Map();
  const child = {
    addEventListener(type, handler) {childListeners.set(type, handler);},
    removeEventListener(type) {childListeners.delete(type);},
    querySelectorAll: () => [],
  };
  const frame = {contentDocument: child, addEventListener(type, handler) {frameListeners.set(type, handler);}, removeEventListener(type) {frameListeners.delete(type);}};
  const opaque = {get contentDocument() {throw Error('cross origin');}, addEventListener() {}, removeEventListener() {}};
  const h = setup({frames: [frame, opaque]}); await flush(); await h.advance(15000);
  frameListeners.get('load')(); await flush();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  childListeners.get('wheel')({isTrusted: false}); await flush();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 1);
  childListeners.get('wheel')({isTrusted: true}); await flush();
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 2);
});

test('idle entry does not automatically acquire another slot', async () => {
  const h = setup({name: 'entry_script', search: '?idle=1'}); await flush();
  assert.equal(h.calls.length, 0); assert.equal(h.find('retry').hidden, false);
  await h.find('retry').onclick();
  assert.equal(h.calls.length, 2); assert.equal(h.calls[1].path, '/v1/personal-cloud/open');
  assert.deepEqual(h.navigations, [['replace', '/maimi/web/chat']]);
});
