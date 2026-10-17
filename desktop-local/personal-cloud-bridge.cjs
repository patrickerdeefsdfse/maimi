'use strict';
// The renderer keeps its original loopback origin. Account authority is held
// here, never accepted from renderer-supplied routing headers or workspace IDs.
const http = require('node:http');
const https = require('node:https');

function createPersonalCloudBridge({ auth, gateway, origin, json, onSwitch = () => {} }) {
  const base = new URL(gateway);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw Error('Invalid cloud gateway');
  let mode = 'local', opening, stopped = false;
  const transports = new Set();
  const alive = () => !stopped && Date.now() < auth.expiresAt;
  const metadata = () => ({ mode, cloud: { name: '云端工作区', pinned: true, deletable: false, manual: true } });
  function closeTransports() {
    for (const value of transports) value.destroy();
    transports.clear();
  }
  function switchMode(next) {
    if (mode !== next) { closeTransports(); onSwitch(); mode = next; }
  }
  async function control(path) {
    const response = await fetch(new URL('/v1/personal-cloud/' + path, base), {
      method: 'POST', redirect: 'error',
      headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },
      body: '{}', signal: AbortSignal.timeout(90000),
    });
    if (!response.ok) throw Object.assign(Error('Cloud unavailable'), { status: response.status });
    const value = await response.json();
    if (!alive()) throw Object.assign(Error('Sign in again'), { status: 401 });
    return value;
  }
  function handleControl(req, res) {
    const path = req.url;
    if (!['/__megmeet/desktop/workspace', '/__megmeet/desktop/cloud/open', '/__megmeet/desktop/local/open'].includes(path)) return false;
    if (!alive()) { json(res, 401, { detail: '请重新登录。' }); return true; }
    if (path.endsWith('/workspace')) {
      json(res, req.method === 'GET' ? 200 : 405, req.method === 'GET' ? metadata() : {});
      return true;
    }
    if (req.method !== 'POST' || req.headers.origin !== origin) { json(res, 403, {}); return true; }
    req.resume();
    if (path.endsWith('/local/open')) {
      // A pending open cannot win over an explicit return to local.
      if (opening) { json(res, 409, { detail: '云端正在连接，请稍后再试。' }); return true; }
      switchMode('local'); json(res, 200, metadata()); return true;
    }
    if (!opening) opening = control('open').then(value => {
      if ((value.status === 'starting' && value.phase === 'preparing') || (value.status === 'queued' && value.phase === 'starting' && value.position === 1)) return { status: 'starting', phase: 'preparing', retry_after: 1 };
      if (value.status === 'queued') {
        if (!Number.isInteger(value.position) || value.position < 1 || value.position > 128) throw Error('Invalid queue');
        return { status: 'queued', position: value.position, retry_after: 5 };
      }
      switchMode('cloud'); return metadata();
    }).finally(() => { opening = undefined; });
    opening.then(value => json(res, ['queued', 'starting'].includes(value.status) ? 202 : 200, value), error => {
      const code = [401, 403, 429, 503].includes(error.status) ? error.status : 503;
      json(res, code, { detail: code === 429 ? '云端当前繁忙，请稍后重试。' : '云端工作区暂时无法连接，本地工作区仍可使用。' });
    });
    return true;
  }
  function headers(req, websocket = false) {
    const result = { Authorization: 'Bearer ' + auth.token, Origin: base.origin };
    for (const name of ['content-type', 'content-length', 'accept', 'range', 'if-none-match']) {
      if (req.headers[name]) result[name] = req.headers[name];
    }
    if (websocket) {
      result.Connection = 'Upgrade'; result.Upgrade = 'websocket';
      for (const name of ['sec-websocket-key', 'sec-websocket-version', 'sec-websocket-protocol'])
        if (req.headers[name]) result[name] = req.headers[name];
    }
    return result;
  }
  function target(raw) {
    if (!raw.startsWith('/') || raw.startsWith('//') || /[\r\n\\#]/.test(raw)) throw Error('Invalid native URL');
    const value = new URL('/v1/personal-cloud/native' + raw, base);
    if (!value.pathname.startsWith('/v1/personal-cloud/native/')) throw Error('Invalid native URL');
    return value;
  }
  function proxyHttp(req, res) {
    if (!alive()) { json(res, 401, { detail: '请重新登录。' }); return; }
    let destination;
    try { destination = target(req.url); } catch { json(res, 400, {}); return; }
    const outgoing = (base.protocol === 'https:' ? https : http).request(destination,
      { method: req.method, headers: headers(req) }, response => {
        const reply = { ...response.headers, 'cache-control': 'no-store' };
        delete reply['set-cookie']; delete reply['connection'];
        // Native redirects must stay within the authenticated local renderer.
        if (reply.location && (!reply.location.startsWith('/') || reply.location.startsWith('//') || /[\\\r\n]/.test(reply.location))) {
          response.destroy(); json(res, 502, { detail: '云端返回了无效跳转。' }); return;
        }
        res.writeHead(response.statusCode, reply); response.pipe(res);
      });
    transports.add(outgoing); outgoing.once('close', () => transports.delete(outgoing));
    outgoing.setTimeout(130000, () => outgoing.destroy());
    outgoing.on('error', () => { if (!res.headersSent) json(res, 502, { detail: '云端连接中断，请重试。' }); else res.end(); });
    res.once('close', () => outgoing.destroy()); req.once('aborted', () => outgoing.destroy()); req.pipe(outgoing);
  }
  function proxyUpgrade(req, socket, head) {
    if (!alive() || req.url.split('?')[0] !== '/api/remote.mux') { socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return; }
    const request = (base.protocol === 'https:' ? https : http).request(target(req.url), { headers: headers(req, true) });
    transports.add(request); request.once('close', () => transports.delete(request));
    request.setTimeout(15000, () => request.destroy());
    request.once('upgrade', (response, upstream, upstreamHead) => {
      request.setTimeout(0);
      transports.add(socket); transports.add(upstream);
      const responseHeaders = Object.entries(response.headers).filter(([key]) => ['upgrade', 'connection', 'sec-websocket-accept', 'sec-websocket-protocol'].includes(key));
      socket.write('HTTP/1.1 101 Switching Protocols\r\n' + responseHeaders.map(([k, v]) => k + ': ' + v).join('\r\n') + '\r\n\r\n');
      if (upstreamHead.length) socket.write(upstreamHead);
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
      socket.once('close', () => { transports.delete(socket); upstream.destroy(); });
      upstream.once('close', () => { transports.delete(upstream); socket.destroy(); });
      upstream.on('error', () => socket.destroy()); socket.on('error', () => upstream.destroy());
    });
    request.once('response', response => { response.resume(); socket.end('HTTP/1.1 ' + response.statusCode + ' Rejected\r\nConnection: close\r\n\r\n'); });
    request.on('error', () => socket.destroy()); socket.once('close', () => request.destroy()); request.end();
  }
  return { handleControl, proxyHttp, proxyUpgrade, isCloud: () => mode === 'cloud',
    refreshModels: () => control('models/refresh'), stop() { stopped = true; closeTransports(); } };
}
module.exports = { createPersonalCloudBridge };
