'use strict';

const fs = require('node:fs');

const path = require('node:path');

const crypto = require('node:crypto');

const http = require('node:http');

const https = require('node:https');

const net = require('node:net');

const { spawn } = require('node:child_process');

const { GATEWAY, jsonRequest } = require('./auth.cjs');

const { MODE } = require('./deployment.cjs');

const { terminateOwnedTree } = require('./process-tree.cjs');

const { createPersonalCloudBridge } = require('./personal-cloud-bridge.cjs');

const { officePatch, desktopPython } = require('./office-boundary.cjs');

const { createOfficeBridge } = require('./office-bridge.cjs');

const { diagnosticLog } = require('./diagnostic-log.cjs');
const { CHAT_MODEL, readChatBody, modelCatalog } = require('./model-boundary.cjs');
const { modelPolicyPatch } = require('./dsh-adapter.cjs');

const { pathToFileURL } = require('node:url');

const { verifyToolkit } = require('./toolkit-boundary.cjs');
const { createProjectArtifactProxy } = require('./project-artifact-proxy.cjs');
const { desktopSearchPatch, requestContext } = require('./desktop-search.cjs');



const { VERSION, validateRuntime, modelPatch, modelSettingsUpdates, communityPatch, launchArguments, startupToken, guardDataFormat, prepareModelProfile, prepareDesktopCommunityProfile } = require('./dsh-adapter.cjs');

const { acquireAccountLease } = require('./account-lease.cjs');

const sha = value => crypto.createHash('sha256').update(value).digest('hex');

function actorKey(userNo) {

  if (typeof userNo !== 'string' || !userNo || userNo !== userNo.trim()) throw Error('无效的账号标识。');

  return sha('iam:' + userNo).slice(0, 32);

}

function findRuntime(resourcesPath = process.resourcesPath) {

  if (MODE !== 'company' && process.env.MEGMEET_CANDIDATE_RUNTIME) {

    return validateRuntime(path.resolve(process.env.MEGMEET_CANDIDATE_RUNTIME));

  }

  const candidates = [path.join(__dirname, 'runtime')];

  if (resourcesPath) candidates.unshift(path.join(resourcesPath, 'runtime'));

  const root = candidates.find(dir => fs.existsSync(path.join(dir, 'runtime-manifest.json')));

  if (!root) throw Error('本机 DSH 运行核心尚未安装。请先运行 Install-Runtime.ps1。');

  return validateRuntime(root);

}

async function listen(server) {

  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });

  return server.address().port;

}

async function unusedPort() {

  const holder = net.createServer();

  const port = await listen(holder);

  await new Promise(resolve => holder.close(resolve));

  return port;

}

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

const UUID = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}';

function enterprisePath(method, raw) {

  if (raw.includes('%') || raw.includes('#') || !raw.startsWith('/__megmeet/enterprise/')) return null;

  const suffix = raw.slice('/__megmeet/enterprise'.length);

  if ((method === 'GET' && /^\/personal-cloud\/history\/trash(?:\?page=\d{1,5})?$/.test(suffix)) ||

      (method === 'POST' && suffix === '/personal-cloud/history/restore')) return '/v1' + suffix;

  if (method === 'GET' && suffix === '/plugins') return '/v1/plugins/catalog';

  if (method === 'GET' && new RegExp(`^/plugins/results\\?native_session_id=(?:session-)?${UUID}&location=(?:local|cloud)$`).test(suffix))

    return '/v1' + suffix;

  if (method === 'POST' && /^\/plugins\/(?:uploads|tasks)$/.test(suffix)) return '/v1' + suffix;

  if (method === 'GET' && suffix === '/connectors/catalog') return '/v1' + suffix;

  if (method === 'POST' && suffix === '/connectors/dingtalk/disconnect') return '/v1' + suffix;
  if (method === 'POST' && suffix === '/connectors/dingtalk/desktop-handoff') return '/v1' + suffix;
  if (method === 'GET' && suffix === '/connectors/dingtalk/cli/status') return '/v1' + suffix;
  if (method === 'POST' && ['/connectors/dingtalk/cli/login','/connectors/dingtalk/cli/disconnect'].includes(suffix)) return '/v1' + suffix;
  return null;

}




async function startHost(options) {

  const runtime=options.runtime||findRuntime();

  const home=path.join(options.stateRoot,'accounts',actorKey(options.auth.user.user_no));

  fs.mkdirSync(home,{recursive:true});

  const lease=await acquireAccountLease(home,runtime.root,{singleInstanceHeld:options.singleInstanceHeld});

  try {

    const host=await startHostOwned({...options,runtime,accountLease:lease});

    const stop=host.stop;

    host.stop=async()=>{await stop();await lease.assertQuiescent();lease.release();};

    return host;

  } catch(error) {

    // If startup never spawned our child, release our own fresh lock even

    // when the probe found an older runtime. Existing stale locks are never

    // removed. Once a child exists, require actual termination evidence.

    try{if(lease.hasChild())await lease.assertQuiescent();lease.release();}catch{}

    throw error;

  }

}

async function startHostOwned({ auth, stateRoot, log, signal, approveTool, runtime, accountLease }) {

  log = diagnosticLog(log);

  signal?.throwIfAborted();

  if (process.platform !== 'win32') throw Error('此桌面运行核心仅适用于 Windows。');

  let catalog = modelCatalog((await jsonRequest('/v1/personal-model/models', { token: auth.token, signal })).value);

  signal?.throwIfAborted();

  const account = actorKey(auth.user.user_no);

  const home = path.join(stateRoot, 'accounts', account);

  const dshHome = path.join(home, 'dsh');

  fs.mkdirSync(dshHome, { recursive: true });

  await guardDataFormat(dshHome,{runtimeRoot:runtime.root,assertQuiescent:()=>accountLease.assertQuiescent()});

  fs.mkdirSync(path.join(home, 'cache'), { recursive: true });

  const release = JSON.parse(fs.readFileSync(path.join(runtime.root, 'release-manifest.json'), 'utf8'));

  const modelLease = crypto.randomBytes(32).toString('base64url');

  const officeMode = MODE === 'company' || fs.existsSync(path.join(runtime.root, 'office-remote/office-remote-manifest.json')) ? 'remote' : 'local';

  const officeToken = crypto.randomBytes(32).toString('base64url');

  const handleOffice = createOfficeBridge({ gateway: GATEWAY, auth, token: officeToken, signal,

    alive: () => !stopping && !cloudBridge.isCloud() });

  const toolkitRoot = verifyToolkit(runtime.root);

  const [{createToolkit},{createDesktopTools},{createToolkitHttp,toolkitPatch}]=await Promise.all([

    import(pathToFileURL(path.join(toolkitRoot,'session-tools.mjs'))),

    import(pathToFileURL(path.join(toolkitRoot,'desktop.mjs'))),

    import(pathToFileURL(path.join(toolkitRoot,'http.mjs'))),

  ]);

  const toolToken=crypto.randomBytes(32).toString('base64url');

  const toolContext={clientKey:sha(auth.token),signal};

  const toolkit=createToolkit({

    authorize:async()=>{if(stopping||signal?.aborted||Date.now()>=auth.expiresAt)throw Object.assign(Error('SIGN_IN_REQUIRED'),{status:401});},

    local:createDesktopTools({python:desktopPython(runtime.root,{mode:officeMode}),approve:approveTool,alive:()=>!stopping&&!signal?.aborted&&Date.now()<auth.expiresAt}),

    remote:async(route,body,context)=>{

      if(!['/catalog','/invoke','/confirm'].includes(route))throw Error('CONNECTOR_ROUTE_DENIED');

      const response=await fetch(GATEWAY+'/v1/connectors'+route,{method:body?'POST':'GET',redirect:'error',

        headers:{Authorization:'Bearer '+auth.token,'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{}),

        signal:AbortSignal.any([context.signal,AbortSignal.timeout(120000)].filter(Boolean))});

      const value=await response.json();if(!response.ok)throw Object.assign(Error(typeof value.detail==='string'?value.detail:'CONNECTOR_UNAVAILABLE'),{status:response.status});return value;

    },

  });

  const toolRequests = new WeakMap();
  const handleTools=createToolkitHttp(toolkit,{token:toolToken,browserContext:req=>toolRequests.get(req)||toolContext,modelContext:req=>toolRequests.get(req)||toolContext});

  const childPort = await unusedPort();

  const childBase = `http://127.0.0.1:${childPort}`;

  let cookie = '', ready = false, child, childExited = false;

  let startup = '';

  let stopping;

  const sockets = new Set();

  const nativeSockets = new Set();

  const publicServer = http.createServer();

  const publicPort = await listen(publicServer);

  const origin = `http://127.0.0.1:${publicPort}`;

  const authority = `127.0.0.1:${publicPort}`;

  const browserCookie = { name: 'megmeet_windows_' + publicPort, value: crypto.randomBytes(32).toString('base64url') };

  const browserAuthorized = req => (req.headers.cookie || '').split(';').some(value => value.trim() === browserCookie.name + '=' + browserCookie.value);

  const allowed = req => req.headers.host === authority

    && (!req.headers.origin || req.headers.origin === origin);

  const json = (res, code, body) => { res.writeHead(code, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(body)); };

  const proxy = (req, res, target, headers, checkedBody) => {

    const transport = new URL(target).protocol === 'https:' ? https : http;

    const outgoing = transport.request(target, { method: req.method, headers }, response => {

      const replyHeaders = { ...response.headers }; delete replyHeaders['set-cookie'];

      res.writeHead(response.statusCode, replyHeaders); response.pipe(res);

    });

    outgoing.setTimeout(130000, () => outgoing.destroy());

    outgoing.on('error', () => { if (!res.headersSent) res.writeHead(502); res.end('Service unavailable'); });

    res.on('close', () => outgoing.destroy());

    req.on('aborted', () => outgoing.destroy()); if (checkedBody) outgoing.end(checkedBody); else req.pipe(outgoing);

  };

  const cloudBridge = createPersonalCloudBridge({ auth, gateway: GATEWAY, origin, json,

    onSwitch: () => { void toolkit.stopAll(); for (const socket of nativeSockets) socket.destroy(); nativeSockets.clear(); } });

  const projectArtifacts=createProjectArtifactProxy({gateway:GATEWAY,auth,origin,browserAuthorized,alive:()=>!stopping&&!signal?.aborted&&cloudBridge.isCloud()});
  let refreshModelsInFlight;

  let appliedCatalog;

  async function applyModelCatalog(next) {

    if (JSON.stringify(next) === appliedCatalog) { catalog = next; return; }

    // The versioned DSH settings API emits its own model-catalog invalidation.

    // Only managed provider/default fields change; native sessions stay open.

    for (const item of modelSettingsUpdates(next, origin)) {

      const response = await fetch(childBase + '/api/' + item.method, { method: 'POST', redirect: 'error',

        headers: { Cookie: cookie, Origin: childBase, 'Content-Type': 'application/json' },

        signal: AbortSignal.timeout(15000),

        body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method: item.method,

          payload: { args: item.args } }) });

      const value = await response.json();

      if (!response.ok || value.result?.ok !== true) throw Error('模型目录同步失败，请重试。');

    }

    catalog = next;

    appliedCatalog = JSON.stringify(next);

  }

  function refreshModels() {

    if (refreshModelsInFlight) return refreshModelsInFlight;

    refreshModelsInFlight = (async () => {

      if (Date.now() >= auth.expiresAt) throw Error('登录已过期，请重新登录。');

      const next = modelCatalog((await jsonRequest('/v1/personal-model/models', { token: auth.token, signal })).value);

      signal?.throwIfAborted();

      if (stopping || Date.now() >= auth.expiresAt) throw Error('登录已过期，请重新登录。');

      await applyModelCatalog(next);

      return catalog;

    })().finally(() => { refreshModelsInFlight = undefined; });

    return refreshModelsInFlight;

  }

  publicServer.on('request', (req, res) => {

    if (!allowed(req)) { json(res, 403, { detail: 'local desktop origin required' }); return; }

    const pathname = req.url.split('?')[0];

    if (pathname.startsWith('/__megmeet/office/')) { handleOffice.handle(req, res); return; }

    if (!pathname.startsWith('/__megmeet/model/') && pathname!=='/__megmeet/tool-execute' && !browserAuthorized(req)) {

      json(res, 401, { detail: 'desktop browser session required' }); return;

    }

    if(pathname==='/__megmeet/tool-execute'||pathname.startsWith('/__megmeet/tools/')){

      if(pathname!=='/__megmeet/tool-execute'&&req.method==='POST'&&req.headers.origin!==origin){json(res,403,{detail:'desktop origin required'});return;}

      if(cloudBridge.isCloud()&&pathname!=='/__megmeet/tool-execute'){cloudBridge.proxyHttp(req,res);return;}

      if(cloudBridge.isCloud()){json(res,409,{detail:'local tools inactive in cloud workspace'});return;}

      const requestScope=requestContext(req,res,toolContext); toolRequests.set(req,requestScope.context);
      void handleTools(req,res).catch(()=>{if(!res.headersSent&&!res.destroyed)json(res,503,{detail:'tool unavailable'});else if(!res.destroyed)res.end();}).finally(()=>{requestScope.dispose();toolRequests.delete(req);});return;

    }

    if (projectArtifacts.handle(req, res)) return;
    if (cloudBridge.handleControl(req, res)) return;

    if (pathname.startsWith('/__megmeet/enterprise/')) {
      // Human UI only: the existing browser cookie is checked above and a
      // same-origin POST is required before attaching the desktop credential.
      if ((pathname === '/__megmeet/enterprise/connectors/dingtalk/desktop-handoff' ||
          (req.method === 'POST' && ['/__megmeet/enterprise/connectors/dingtalk/cli/login','/__megmeet/enterprise/connectors/dingtalk/cli/disconnect'].includes(pathname))) && req.headers.origin !== origin) {
        json(res, 403, { detail: 'desktop origin required' }); return;
      }
      const target = enterprisePath(req.method, req.url);

      if (!target) { json(res, 404, {}); return; }

      if (Date.now() >= auth.expiresAt) { json(res, 401, { detail: 'sign in again' }); return; }

      const chunks = []; let size = 0, rejected = false;

      req.on('data', chunk => {

        size += chunk.length;

        if (size > 512 * 1024) { if (!rejected) json(res, 413, { detail: 'attachment or request too large' }); rejected = true; return; }

        if (!rejected) chunks.push(chunk);

      });

      req.on('end', async () => {

        if (rejected) return;

        const disconnected = new AbortController();

        res.once('close', () => disconnected.abort());

        try {

          const response = await fetch(GATEWAY + target, { method: req.method, redirect: 'error',

            headers: { Authorization: 'Bearer ' + auth.token, 'Content-Type': 'application/json' },

            ...(['POST', 'PUT'].includes(req.method) ? { body: Buffer.concat(chunks) } : {}), signal: AbortSignal.any([disconnected.signal, AbortSignal.timeout(120000)]) });

          res.writeHead(response.status, { 'Content-Type': response.headers.get('content-type') || 'application/json', 'Cache-Control': 'no-store' });

          for await (const chunk of response.body || []) { if (res.destroyed) break; res.write(chunk); }

          res.end();

        } catch { if (!res.headersSent) json(res, 502, { detail: 'enterprise service unavailable' }); else res.end(); }

      });

      return;

    }

    if (pathname.startsWith('/__megmeet/model/')) {

      if (req.headers.authorization !== 'Bearer ' + modelLease || Date.now() >= auth.expiresAt) {

        json(res, 401, { error: { message: 'Desktop session expired. Sign in again.' } }); return;

      }

      const targetPath = pathname.slice('/__megmeet/model'.length);

      if (!['/v1/chat/completions', '/v1/models'].includes(targetPath)) { json(res, 404, {}); return; }

      if (targetPath === '/v1/models') {
        if (req.method !== 'GET') { json(res, 405, {}); return; }
        json(res, 200, {data:catalog.models,default_model:catalog.defaultModel}); return;
      }
      if (req.method !== 'POST' || req.headers['content-encoding']) { json(res, 400, {error:{message:'CHAT_REQUEST_INVALID'}}); return; }
      readChatBody(req, () => catalog.models.some(row => row.id === CHAT_MODEL)).then(bytes => {
        if (Date.now() >= auth.expiresAt) { json(res, 401, {error:{message:'Desktop session expired. Sign in again.'}}); return; }
        proxy(req, res, GATEWAY + '/v1/personal-model/chat/completions', {
          'Content-Type':'application/json', 'Content-Length':String(bytes.length), Authorization:'Bearer '+auth.token,
        }, bytes);
      }, error => { if (!res.destroyed) json(res, error.status || 400, {error:{message:error.code || 'CHAT_REQUEST_INVALID'}}); });

      return;

    }

    if (pathname === '/__megmeet/health') {

      json(res, 200, { status: ready ? 'ready' : 'starting', dsh: VERSION, frontend: release.frontend,

        mode: MODE, account, releaseId: release.releaseId }); return;

    }

    if (pathname === '/__megmeet/release') { json(res, 200, release); return; }

    if (pathname === '/__megmeet/desktop/session') {

      json(res, 200, { user: auth.user, mode: MODE, expiresAt: auth.expiresAt }); return;

    }

    if (pathname === '/__megmeet/desktop/models' || pathname === '/__megmeet/desktop/models/refresh') {

      if (Date.now() >= auth.expiresAt) { json(res, 401, { detail: 'sign in again' }); return; }

      if (req.url !== pathname) { json(res, 400, {}); return; }

      if (pathname.endsWith('/refresh')) {

        if (req.method !== 'POST' || req.headers.origin !== origin) { json(res, 403, {}); return; }

        if (!ready) { json(res, 503, { detail: 'local DSH starting' }); return; }

        req.resume();

        (cloudBridge.isCloud() ? cloudBridge.refreshModels() : refreshModels()).then(value => { catalog = value; json(res, 200, value); }, () => json(res, 503, { detail: '个人模型目录暂时无法刷新，请重试；当前请求仍由服务器核验模型权限。' }));

      } else if (req.method === 'GET') json(res, 200, catalog);

      else json(res, 405, {});

      return;

    }

    if (cloudBridge.isCloud()) { cloudBridge.proxyHttp(req, res); return; }

    if (!ready) { json(res, 503, { detail: 'local DSH starting' }); return; }

    const headers = { ...req.headers, host: `127.0.0.1:${childPort}`, origin: childBase, cookie };

    delete headers.authorization;

    proxy(req, res, childBase + req.url, headers);

  });

  publicServer.on('connection', socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); });

  publicServer.on('upgrade', (req, socket, head) => {

    if (!ready || !allowed(req) || !browserAuthorized(req) || req.url.split('?')[0] !== '/api/remote.mux') {

      socket.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return;

    }

    if (cloudBridge.isCloud()) { cloudBridge.proxyUpgrade(req, socket, head); return; }

    nativeSockets.add(socket); socket.once('close', () => nativeSockets.delete(socket));

    const upstream = net.connect(childPort, '127.0.0.1', () => {

      const headers = { ...req.headers, host: `127.0.0.1:${childPort}`, origin: childBase, cookie };

      delete headers.authorization;

      upstream.write(req.method + ' ' + req.url + ' HTTP/1.1\r\n'

        + Object.entries(headers).map(([key, value]) => key + ': ' + value).join('\r\n') + '\r\n\r\n');

      if (head.length) upstream.write(head);

      socket.pipe(upstream).pipe(socket);

    });

    upstream.on('error', () => socket.destroy()); socket.on('error', () => upstream.destroy());

    socket.on('close', () => upstream.destroy()); upstream.on('close', () => socket.destroy());

  });

  const patch = path.join(home, 'desktop-profile.patch.json');

  const community = path.join(home, 'community-profile.patch.json');

  try {

    await prepareModelProfile(dshHome,runtime.root,catalog,origin);

    await prepareDesktopCommunityProfile(dshHome,runtime.root);

  } catch(error) { publicServer.close();handleOffice.stop();cloudBridge.stop();projectArtifacts.stop();await toolkit.close();throw error; }

  const patchContents = JSON.stringify([...modelPatch(catalog, origin), {id:'skill-office',config:{node:runtime.node,cli:false}}, officePatch(runtime.root, {

    mode: officeMode, endpoint: origin + '/__megmeet/office', token: officeToken }),toolkitPatch(toolkitRoot,origin+'/__megmeet/tool-execute',toolToken),...desktopSearchPatch(home,origin+'/__megmeet/tool-execute',toolToken),...modelPolicyPatch(home,origin+'/__megmeet/model/v1')], null, 2);

  try { fs.writeFileSync(patch, patchContents); fs.writeFileSync(community, JSON.stringify(communityPatch(runtime.root))); }

  catch { publicServer.close(); throw Error('本机账号运行配置无法写入。'); }

  const environment = { ...process.env, DSH_HOME: dshHome, DSH_TELEMETRY_DISABLED: '1', DSH_TELEMETRY_MODE: 'DISABLED',

    XDG_CACHE_HOME: path.join(home, 'cache'), MEGMEET_MODEL_TOKEN: modelLease };

  delete environment.ELECTRON_RUN_AS_NODE;

  delete environment.DEEPSEEK_API_KEY; delete environment.DEEPSEEK_BASE_URL;

  delete environment.OPENAI_API_KEY; delete environment.OPENAI_BASE_URL;

  delete environment.MEGMEET_OFFICE_ENDPOINT; delete environment.MEGMEET_OFFICE_TOKEN;

  if (officeMode === 'remote') { environment.MEGMEET_OFFICE_ENDPOINT = origin + '/__megmeet/office'; environment.MEGMEET_OFFICE_TOKEN = officeToken; }

  child = spawn(runtime.node, launchArguments(runtime.root, { patch, community, port: childPort, authority }), { cwd: home, env: environment,

    windowsHide: true, shell: false, stdio: ['ignore', 'pipe', 'pipe'] });

  accountLease.setChild(child);

  child.stdout.on('data', data => { startup = (startup + data.toString()).slice(-65536); });

  child.stderr.on('data', data => { startup = (startup + data.toString()).slice(-65536); });

  child.on('error', () => { childExited = true; });

  child.on('exit', code => { childExited = true; ready = false; log?.write(`WINDOWS_DSH_EXIT code=${code}\n`); });

  const abort = () => { void stop(); };

  signal?.addEventListener('abort', abort, { once: true });

  function stop() {

    if (stopping) return stopping;

    stopping = (async () => {

    signal?.removeEventListener('abort', abort);

    ready = false;

    cloudBridge.stop();projectArtifacts.stop();

    handleOffice.stop();

    await toolkit.close();

    for (const socket of sockets) socket.destroy();

    publicServer.close();

    if (child && !childExited) {

      // Only terminate the child process this launcher created; never hunt by name.

      await terminateOwnedTree(child, () => childExited);

      for (let n = 0; n < 40 && !childExited; n++) await delay(100);

      if (!childExited) child.kill('SIGKILL');

    }

    cookie = '';

    })();

    return stopping;

  }

  try {

    for (let n = 0; n < 480 && !childExited; n++) {

      signal?.throwIfAborted();

      const token = startupToken(startup, childPort);

      if (token) {

        const response = await fetch(childBase + '/?token=' + token, { redirect: 'manual' });

        cookie = response.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');

        if (cookie) break;

      }

      await delay(500);

    }

    if (!cookie) {

      const sanitized = startup.replace(/token=[A-Za-z0-9_-]+/g, 'token=[redacted]')

        .replaceAll(modelLease, '[local-lease-redacted]').replaceAll(officeToken, '[office-lease-redacted]');

      log?.write(sanitized.split('\n').slice(-18).join('\n') + '\n');

      throw Error('Windows 本机 DSH 启动失败，详情见本机启动日志。');

    }

    signal?.throwIfAborted();

    await applyModelCatalog(catalog);

    signal?.throwIfAborted();

    ready = true;

    log?.write(`WINDOWS_DSH_READY version=${VERSION} account=${account} workspace=manual\n`);

    return { origin, account, home, dshHome, release, pid: child.pid, browserCookie, stop,stopTools:()=>toolkit.stopAll() };

  } catch (error) { await stop(); throw error; }

}



module.exports = { VERSION, actorKey, findRuntime, startHost, enterprisePath, modelCatalog };

