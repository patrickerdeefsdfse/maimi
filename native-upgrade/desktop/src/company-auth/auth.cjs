'use strict';
const crypto = require('node:crypto');
const { GATEWAY, MODE } = require('./deployment.cjs');

async function jsonRequest(path, { body, token, signal } = {}) {
  const response = await fetch(GATEWAY + path, {
    method: body === undefined ? 'GET' : 'POST', redirect: 'error', signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000),
    headers: { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...(token ? { Authorization: 'Bearer ' + token } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let value;
  try { value = await response.json(); } catch {
    throw Object.assign(Error('登录服务返回格式无效。'), {status: response.status});
  }
  if (!response.ok && response.status !== 202) {
    const error = Error('登录服务暂不可用，请稍后重试。');
    if (typeof value.detail === 'string' && /^desktop_[a-z_]{1,80}$/.test(value.detail)) error.code = value.detail;
    error.status = response.status; throw error;
  }
  return { status: response.status, value };
}

async function signIn(openBrowser, { signal, onStatus = () => {}, device } = {}) {
  const verifier = device?.verifier || crypto.randomBytes(32).toString('base64url');
  const instanceId = device?.instanceId || crypto.randomUUID();
  const challenge = crypto.createHash('sha256').update(verifier).digest('base64url');
  const { value: flow } = await jsonRequest('/v1/desktop/auth/start', {
    body: { instance_id: instanceId, code_challenge: challenge, ...(device ? {remember_device: true} : {}) }, signal,
  });
  const browserUrl = new URL(flow.browser_url);
  if (browserUrl.origin !== GATEWAY || browserUrl.username || browserUrl.password
      || browserUrl.pathname !== '/v1/desktop/auth/authorize' || browserUrl.hash
      || typeof flow.flow_id !== 'string' || !Number.isFinite(flow.expires_in)) {
    throw Error('登录服务的跳转目标无效。');
  }
  if (!/^[A-F0-9]{4}-[A-F0-9]{4}$/.test(flow.verification_code)) throw Error('登录验证码无效。');
  onStatus('请在系统浏览器完成登录。仅在浏览器与此处验证码一致时确认：' + flow.verification_code);
  await openBrowser(browserUrl.href, { verificationCode: flow.verification_code });
  const deadline = Date.now() + Math.min(flow.expires_in, 600) * 1000;
  while (Date.now() < deadline) {
    signal?.throwIfAborted();
    const { status, value } = await jsonRequest('/v1/desktop/auth/exchange', {
      body: { flow_id: flow.flow_id, instance_id: instanceId, code_verifier: verifier,
        ...(device ? {device_id: device.deviceId, resume_token: device.token, request_id: device.enrollmentId} : {}) }, signal,
    });
    if (status === 200) {
      if (value.token_type?.toLowerCase() !== 'bearer' || typeof value.access_token !== 'string'
          || !value.access_token || !Number.isFinite(value.expires_in) || value.expires_in <= 0 || value.expires_in > 900
          || typeof value.user?.user_no !== 'string' || !value.user.user_no.trim()) {
        throw Error('登录身份不完整。');
      }
      return { token: value.access_token, expiresAt: Date.now() + value.expires_in * 1000,
        ...(value.renewal_supported === true ? { renewal: { instanceId, verifier } } : {}),
        ...(device && value.resume_supported === true ? {remembered: rememberedResult(value, device)} : {}),
        user: value.user, mode: MODE };
    }
    await new Promise((resolve, reject) => {
      const abort = () => { clearTimeout(timer); reject(signal.reason); };
      const timer = setTimeout(() => { signal?.removeEventListener('abort', abort); resolve(); }, 1000);
      signal?.addEventListener('abort', abort, { once: true });
    });
  }
  throw Error('登录等待超时，请重试。');
}

function rememberedResult(value, device) {
  // Zero is the server's explicit until-revoked device grant. Access tokens
  // still carry their short finite expiry and rotate on every resume.
  if (value.device_id !== device.deviceId || !Number.isSafeInteger(value.device_expires_at)
      || value.device_expires_at < 0 || (value.device_expires_at !== 0
      && (value.device_expires_at * 1000 <= Date.now() || value.device_expires_at * 1000 > Date.now() + 31 * 86400000)))
    throw Error('设备登录返回无效。');
  return {deviceId: value.device_id, expiresAt: value.device_expires_at * 1000};
}

async function capabilities({signal} = {}) {
  try {
    const {value} = await jsonRequest('/v1/desktop/auth/session?capabilities=remembered-login-v1', {signal});
    return value.remembered_login === true && value.protocol === 1;
  } catch (error) {
    // Older servers protect session without knowing this capability query.
    // This unauthenticated discovery rejection does not invalidate a device.
    if ([401, 403, 404, 405].includes(error.status)) return false;
    throw error;
  }
}

async function resumeDevice(device, {signal} = {}) {
  if (!device.pending) throw Error('设备续登尚未准备。');
  const started = Date.now();
  const {value} = await jsonRequest('/v1/desktop/auth/renew', {signal, body: {
    kind: 'device_resume',
    device_id: device.deviceId, instance_id: device.instanceId, code_verifier: device.verifier,
    resume_token: device.token, next_resume_token: device.pending.nextToken, request_id: device.pending.requestId,
  }});
  if (value.token_type?.toLowerCase() !== 'bearer' || typeof value.access_token !== 'string' || !value.access_token
      || !Number.isInteger(value.expires_in) || value.expires_in <= 0 || value.expires_in > 900
      || typeof value.user?.user_no !== 'string' || !value.user.user_no.trim()
      || (device.user && (value.user.user_no !== device.user.user_no || value.user.account_no !== device.user.account_no)))
    throw Error('设备续登身份无效。');
  return {token: value.access_token, expiresAt: started + value.expires_in * 1000,
    renewal: {instanceId: device.instanceId, verifier: device.verifier},
    remembered: rememberedResult(value, device), user: value.user, mode: MODE};
}

async function revokeDevice(device, {signal} = {}) {
  const {value} = await jsonRequest('/v1/desktop/auth/logout', {signal, body: {
    kind: 'device_logout',
    device_id: device.deviceId, instance_id: device.instanceId, code_verifier: device.verifier, resume_token: device.token,
  }});
  if (value.revoked !== true) throw Error('设备退出尚未确认。');
}

async function signOut(auth, { timeout = 10000 } = {}) {
  if (!auth) return;
  await jsonRequest('/v1/desktop/auth/logout', { body: {}, token: auth.token,
    signal: AbortSignal.timeout(timeout) });
}

async function renewSession(auth, { signal } = {}) {
  if (!auth?.renewal) throw Error('当前登录不支持自动续期。');
  const started = Date.now();
  const { value } = await jsonRequest('/v1/desktop/auth/renew', {
    token: auth.token, signal, body: { instance_id: auth.renewal.instanceId, code_verifier: auth.renewal.verifier },
  });
  if (!Number.isInteger(value.expires_in) || value.expires_in <= 0 || value.expires_in > 900 ||
      value.user?.user_no !== auth.user.user_no || value.user?.account_no !== auth.user.account_no) {
    throw Error('登录续期返回无效。');
  }
  return started + value.expires_in * 1000;
}

// Proof-bound application renewal cannot outlive the original IAM lease. The
// same object is retained so active model/cloud bridges see the new deadline.
function createSessionExpiry({ onExpire, renew, now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let current, timer, renewalAbort, renewing;
  function clear() {
    if (timer !== undefined) clearTimer(timer);
    renewalAbort?.abort(); renewalAbort = undefined; renewing = undefined;
    timer = undefined; current = undefined;
  }
  function check() {
    if (!current || now() < current.expiresAt) return false;
    const expired = current; clear(); onExpire(expired); return true;
  }
  function schedule() {
    if (!current) return;
    const remaining = current.expiresAt - now();
    const canRenew = typeof renew === 'function' && current.renewal;
    const wait = canRenew ? Math.min(remaining, Math.max(30000, remaining - 120000)) : remaining;
    timer = setTimer(() => { timer = undefined; if (!check()) void refresh(); }, Math.max(0, Math.min(wait, 2147483647)));
  }
  async function refresh() {
    if (!current || check()) return;
    if (!current.renewal || typeof renew !== 'function') { schedule(); return; }
    if (renewing) return renewing;
    const target = current, controller = new AbortController(); renewalAbort = controller;
    if (timer !== undefined) clearTimer(timer);
    timer = setTimer(() => { timer = undefined; check(); }, Math.max(0, target.expiresAt - now()));
    const work = (async () => {
      try {
        const deadline = await renew(target, { signal: controller.signal });
        if (current === target && !controller.signal.aborted && target.expiresAt > now() &&
            Number.isFinite(deadline) && deadline > now() && deadline <= now() + 900000) {
          target.expiresAt = deadline;
        }
      } catch (error) {
        if (current === target && !controller.signal.aborted && [401, 403].includes(error.status)) {
          const denied = current; clear(); onExpire(denied); return;
        }
      } finally {
        if (current === target && !controller.signal.aborted) {
          renewalAbort = undefined; renewing = undefined;
          if (timer !== undefined) clearTimer(timer);
          timer = undefined; if (!check()) schedule();
        }
      }
    })();
    renewing = work; return work;
  }
  return { clear, check, refresh, arm(auth) {
    clear();
    if (!auth || !Number.isFinite(auth.expiresAt)) throw Error('登录有效期无效。');
    current = auth; if (!check()) schedule();
  } };
}

module.exports = { GATEWAY, jsonRequest, signIn, signOut, renewSession, createSessionExpiry,
  capabilities, resumeDevice, revokeDevice };
