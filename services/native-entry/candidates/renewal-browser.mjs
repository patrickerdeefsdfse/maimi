/** Local candidate only: NOT registered with Cordis, bundled, or deployed.
 * The caller supplies the already verified identity and native ConnectionHandle.
 * Events only request an identity check; no connection-state enum is assumed.
 * This adapter never sends/replays native RPC, reads tokens, or navigates.
 */
import { createRenewalController } from './renewal-controller.mjs';

const LOCK = 'megmeet-native-renewal-v1';
const CHANNEL = 'megmeet-native-renewal-v1';
const REFRESH = '/v1/auth/refresh-token';
const SIGNAL = Object.freeze({ type: 'credentials-updated' });
const PAUSE = new Set(['authentication-required', 'account-denied']);
const SUCCESS = new Set(['authenticated', 'reconnect-requested']);

export function createRenewalBrowser(options) {
  const { connection, expectedIdentity, onState, onIdentityChanged,
    browser = globalThis, cooldownMs = 15000, timeoutMs = 20000 } = options || {};
  if (!connection || typeof connection.reconnect !== 'function'
      || typeof connection.state?.getSnapshot !== 'function'
      || typeof connection.state?.subscribe !== 'function') {
    throw new TypeError('A native connection handle is required');
  }
  if (typeof onState !== 'function' || typeof onIdentityChanged !== 'function'
      || !Number.isFinite(cooldownMs) || cooldownMs < 1 || cooldownMs > 120000) {
    throw new TypeError('Invalid renewal browser options');
  }
  const document = browser.document;
  for (const [owner, names] of [
    [browser, ['fetch', 'addEventListener', 'removeEventListener', 'setTimeout', 'clearTimeout']],
    [document, ['addEventListener', 'removeEventListener']],
  ]) {
    if (!owner || names.some(name => typeof owner[name] !== 'function')) {
      throw new TypeError('Required browser capability is missing');
    }
  }
  const now = typeof browser.performance?.now === 'function'
    ? () => browser.performance.now() : () => Date.now();
  const timers = { set: browser.setTimeout.bind(browser), clear: browser.clearTimeout.bind(browser) };
  const cleanup = [];
  let disposed = false, identityChanged = false, paused = false, failures = 0;
  let active = false, activeOperation = null, pending = false, credentialsPending = false, timer = null;
  let lastStarted = -Infinity, lastSnapshot = connection.state.getSnapshot();
  let visible = document.visibilityState !== 'hidden';
  let online = browser.navigator?.onLine !== false;
  let channel = null;

  function diagnose(capability) {
    if (disposed) return;
    try { Promise.resolve(onState(Object.freeze({ status: 'capability-unavailable', capability }))).catch(() => {}); }
    catch {}
  }

  // A channel carries a wake-up signal only. Every recipient verifies its own
  // browser Cookie through the server before requesting a new generation.
  if (typeof browser.BroadcastChannel === 'function') {
    try {
      channel = new browser.BroadcastChannel(CHANNEL);
      if (['addEventListener', 'removeEventListener', 'postMessage', 'close']
        .some(name => typeof channel[name] !== 'function')) throw new TypeError('Invalid channel');
    } catch {
      try { channel?.close(); } catch {}
      channel = null;
    }
  }
  const locks = browser.navigator?.locks;
  const canLock = typeof locks?.request === 'function';
  let controller;
  try { controller = createRenewalController({
    expectedIdentity, timeoutMs, onState,
    onIdentityChanged: async identities => {
      identityChanged = true;
      pending = false;
      credentialsPending = false;
      await onIdentityChanged(identities);
    },
    reconnect: () => connection.reconnect(),
    fetcher: async (path, init) => {
      const response = await browser.fetch(path, init);
      if (!disposed && !init.signal.aborted && path === REFRESH && init.method === 'POST'
          && Number.isInteger(response.status) && response.status >= 200 && response.status < 300) {
        // No response body or credential is read, copied, or placed in storage.
        try { channel.postMessage(SIGNAL); } catch { diagnose('broadcast-channel'); }
      }
      return response;
    },
    withRefreshLock: (work, { signal }) => {
      // Without both coordination capabilities, identity GETs remain useful but
      // there is no unlocked or silent single-tab POST-refresh fallback.
      if (!canLock || !channel) throw new Error('Cross-tab renewal unavailable');
      return locks.request(LOCK, { mode: 'exclusive', signal }, lock => {
        if (!lock || lock.name !== LOCK || lock.mode !== 'exclusive') throw new Error('Lock was not granted');
        return work();
      });
    },
  }); } catch (error) {
    try { channel?.close(); } catch {}
    throw error;
  }

  function schedule() {
    if (disposed || identityChanged || paused || !pending || active || timer !== null || !visible || !online) return;
    const delay = Math.max(0, cooldownMs - (now() - lastStarted));
    timer = timers.set(() => {
      timer = null;
      if (disposed || identityChanged || paused || !pending || active || !visible || !online) return;
      const changed = credentialsPending;
      credentialsPending = false;
      pending = false;
      lastStarted = now();
      active = true;
      activeOperation = changed ? controller.credentialsChanged() : controller.check();
      const settle = async () => {
        // A notification can invalidate an in-flight query or, at the commit
        // boundary, join the controller's next serial round. Follow that round
        // before releasing the browser scheduler's single-flight gate.
        for (;;) {
          const watched = activeOperation;
          const result = await watched;
          if (watched === activeOperation) return result;
        }
      };
      settle().then(result => {
        if (disposed || identityChanged) return;
        if (SUCCESS.has(result.status)) failures = 0;
        else failures += 1;
        if (PAUSE.has(result.status) || failures >= 3) paused = true;
      }).catch(() => {
        if (!disposed) { failures += 1; if (failures >= 3) paused = true; }
      }).finally(() => {
        active = false;
        activeOperation = null;
        // Only previously received external events can schedule another round.
        // Completion/failure itself never schedules a retry.
        schedule();
      });
    }, delay);
  }

  function request({ changed = false, wake = false } = {}) {
    if (disposed || identityChanged) return false;
    if (wake) { paused = false; failures = 0; }
    if (changed) {
      // A real credential change may recover an earlier failed round. Do not
      // permanently ignore it, but retain cooldown and server verification.
      paused = false;
      if (active) {
        activeOperation = controller.credentialsChanged();
        return true;
      }
      credentialsPending = true;
    }
    if (paused) return false;
    pending = true;
    schedule();
    return true;
  }

  function listen(owner, event, handler) {
    owner.addEventListener(event, handler);
    cleanup.push(() => owner.removeEventListener(event, handler));
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    pending = false;
    credentialsPending = false;
    if (timer !== null) { timers.clear(timer); timer = null; }
    controller.dispose();
    for (const remove of cleanup.splice(0).reverse()) { try { remove(); } catch {} }
    try { channel?.close(); } catch {}
  }

  try {
    const unsubscribe = connection.state.subscribe(() => {
      if (disposed || identityChanged) return;
      let next;
      try { next = connection.state.getSnapshot(); }
      catch { paused = true; diagnose('native-state'); return; }
      if (Object.is(lastSnapshot, next)) return;
      lastSnapshot = next;
      request();
    });
    if (typeof unsubscribe !== 'function') throw new TypeError('Native state subscription must return cleanup');
    cleanup.push(unsubscribe);
    listen(document, 'visibilitychange', () => {
      const next = document.visibilityState !== 'hidden';
      const waking = !visible && next;
      visible = next;
      if (waking) request({ wake: true });
    });
    listen(browser, 'offline', () => { online = false; });
    listen(browser, 'online', () => {
      const waking = !online;
      online = browser.navigator?.onLine !== false;
      if (waking && online) request({ wake: true });
    });
    if (channel) listen(channel, 'message', event => {
      const data = event.data;
      if (data && typeof data === 'object' && !Array.isArray(data)
          && Object.keys(data).length === 1 && data.type === SIGNAL.type) request({ changed: true });
    });
    if (!canLock) diagnose('web-locks');
    if (!channel) diagnose('broadcast-channel');
    request();
  } catch (error) { dispose(); throw error; }

  return Object.freeze({
    // A user-triggered check is queued with the same cooldown, not an RPC retry.
    check: () => request({ wake: true }),
    dispose,
  });
}
