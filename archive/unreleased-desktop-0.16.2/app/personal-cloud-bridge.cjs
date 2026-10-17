'use strict';
// The renderer keeps its original loopback origin. Account authority is held
// here, never accepted from renderer-supplied routing headers or workspace IDs.
const http = require('node:http');
const https = require('node:https');

function createPersonalCloudBridge({ auth, gateway, origin, json, onSwitch = () => {}, beforeLocal = async () => {}, onMode = () => {}, signal }) {
  const base = new URL(gateway);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw Error('Invalid cloud gateway');
  let mode = 'local', opening, localOpening, stopped = false;
  const lifetime = new AbortController();
  const transports = new Set();
  const presentClients = new Set();
  const presenceOperations = new Set();
  let closing;
  const alive = () => !stopped && Date.now() < auth.expiresAt;
  const metadata = () => ({ mode, client: 'desktop', resumed: mode === 'cloud', closed_retention_seconds: 7200,
    cloud: { name: '云端工作区', pinned: true, deletable: false, manual: true } });
  function closeTransports() {
    for (const value of transports) value.destroy();
    transports.clear();
  }
  function switchMode(next) {
    if (mode !== next) { closeTransports(); onSwitch(); mode = next; }
    onMode(next);
  }
  async function control(path, body = {}, timeout = 90000) {
    const response = await fetch(new URL('/v1/personal-cloud/' + path, base), {
      method: 'POST', redirect: 'error',
      headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.any([lifetime.signal, signal, AbortSignal.timeout(timeout)].filter(Boolean)),
    });
    if (!response.ok) throw Object.assign(Error('Cloud unavailable'), { status: response.status });
    const value = await response.json();
    if (!alive()) throw Object.assign(Error('Sign in again'), { status: 401 });
    return value;
  }
  function openCloud() {
    if (!alive()) return Promise.reject(Object.assign(Error('Sign in again'), { status: 401 }));
    if (localOpening) return Promise.reject(Object.assign(Error('Local opening'), {status:409}));
    if (!opening) opening = control('open').then(value => {
      if ((value.status === 'starting' && value.phase === 'preparing') || (value.status === 'queued' && value.phase === 'starting' && value.position === 1)) return { status: 'starting', phase: 'preparing', retry_after: 1 };
      if (value.status === 'queued') {
        if (!Number.isSafeInteger(value.position) || value.position < 1) throw Error('Invalid queue');
        return { status: 'queued', position: value.position, retry_after: 5 };
      }
      switchMode('cloud'); return metadata();
    }).finally(() => { opening = undefined; });
    return opening;
  }
  async function activateCloud() {
    const until = Date.now() + 120000;
    while (alive()) {
      const result = await openCloud();
      if (result.mode === 'cloud') return result;
      if (Date.now() >= until) throw Error('Cloud startup timeout');
      await new Promise((resolve,reject) => {
        const aborted=()=>{clearTimeout(timer);reject(Object.assign(Error('Cancelled'),{name:'AbortError'}));};
        const timer=setTimeout(()=>{lifetime.signal.removeEventListener('abort',aborted);resolve();},result.retry_after*1000);
        lifetime.signal.addEventListener('abort',aborted,{once:true});
        if(lifetime.signal.aborted)aborted();
      });
    }
    throw Object.assign(Error('Sign in again'), {status:401});
  }
  async function presence(req,res) {
    let body='', invalid=false;
    for await (const chunk of req) {body+=chunk;if(body.length>512){invalid=true;break;}}
    let value;
    try { value=JSON.parse(body); } catch {invalid=true;}
    if(invalid||!value||Object.keys(value).sort().join(',')!=='client_id,present'||
      typeof value.present!=='boolean'||typeof value.client_id!=='string'||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value.client_id)) {
      json(res,400,{});return;
    }
    if(mode!=='cloud'){json(res,409,{detail:'personal_cloud_open_required'});return;}
    try {
      if(value.present)presentClients.add(value.client_id);
      const operation=control('presence',{client_id:value.client_id,present:value.present},10000);
      presenceOperations.add(operation);
      let observed;
      try { observed=await operation; }
      finally { presenceOperations.delete(operation); }
      if(!value.present)presentClients.delete(value.client_id);
      json(res,200,{status:'ready',closed_retention_seconds:7200,busy:typeof observed?.busy==='boolean'?observed.busy:null});
    } catch(error) {json(res,[401,403,409,429].includes(error.status)?error.status:503,{detail:'workspace_presence_unavailable'});}
  }
  function handleControl(req, res) {
    const path = req.url;
    if (!['/__megmeet/desktop/workspace', '/__megmeet/desktop/cloud/open', '/__megmeet/desktop/local/open', '/__megmeet/desktop/cloud/presence'].includes(path)) return false;
    if (!alive()) { json(res, 401, { detail: '请重新登录。' }); return true; }
    if (path.endsWith('/workspace')) {
      json(res, req.method === 'GET' ? 200 : 405, req.method === 'GET' ? metadata() : {});
      return true;
    }
    if (req.method !== 'POST' || req.headers.origin !== origin) { json(res, 403, {}); return true; }
    if (path.endsWith('/cloud/presence')) {void presence(req,res).catch(()=>{if(!res.headersSent)json(res,400,{});});return true;}
    req.resume();
    if (path.endsWith('/local/open')) {
      // A pending open cannot win over an explicit return to local.
      if (opening) { json(res, 409, { detail: '云端正在连接，请稍后再试。' }); return true; }
      if(!localOpening)localOpening=Promise.resolve().then(beforeLocal).then(()=>{
        if(!alive())throw Object.assign(Error('Sign in again'),{status:401});
        switchMode('local');return metadata();
      }).finally(()=>{localOpening=undefined;});
      localOpening.then(value=>json(res,200,value),error=>json(res,[401,403].includes(error.status)?error.status:503,{detail:'本机工作区暂时无法打开，请稍后重试。'}));return true;
    }
    openCloud().then(value => json(res, ['queued', 'starting'].includes(value.status) ? 202 : 200, value), error => {
      const code = [401, 403, 409, 429, 503].includes(error.status) ? error.status : 503;
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
  function stop() {
    if(closing)return closing;
    // Retire renderer authority synchronously. Best-effort closure reports can
    // only remove our own presence instances, never extend authority or open a
    // workspace. A crashed/offline app is covered by the server presence TTL.
    stopped=true;lifetime.abort();closeTransports();
    closing=(async()=>{
      await Promise.allSettled([...presenceOperations]);
      await Promise.allSettled([...presentClients].map(client_id=>fetch(new URL('/v1/personal-cloud/presence',base),{
        method:'POST',redirect:'error',headers:{Authorization:'Bearer '+auth.token,'Content-Type':'application/json'},
        body:JSON.stringify({client_id,present:false}),signal:AbortSignal.timeout(2000),
      }).then(response=>response.body?.cancel())));
      presentClients.clear();
    })();
    return closing;
  }
  return { handleControl, proxyHttp, proxyUpgrade, isCloud: () => mode === 'cloud',
    activateCloud, refreshModels: () => control('models/refresh'), stop };
}
module.exports = { createPersonalCloudBridge };
