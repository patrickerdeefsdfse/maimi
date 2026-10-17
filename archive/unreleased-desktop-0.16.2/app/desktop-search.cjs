'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL} = require('node:url');

// Only the shipped adapter is materialized; TokenKey credentials remain in the
// authenticated Electron host. This is an ephemeral loopback capability.
function desktopSearchPatch(home, endpoint, token) {
  const base = new URL(endpoint);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port
      || base.pathname !== '/__megmeet/native-web' || base.search || base.hash
      || base.username || base.password || !/^[A-Za-z0-9_-]{43}$/.test(token || ''))
    throw Error('SEARCH_PRIVATE_ENDPOINT_REQUIRED');
  const directory = path.join(home, 'megmeet-managed-plugins');
  fs.mkdirSync(directory, {recursive: true});
  if (!fs.lstatSync(directory).isDirectory() || fs.lstatSync(directory).isSymbolicLink()) throw Error('SEARCH_ADAPTER_PATH_INVALID');
  const file = path.join(directory, 'desktop-search-plugin.mjs');
  if (fs.existsSync(file) && (!fs.lstatSync(file).isFile() || fs.lstatSync(file).isSymbolicLink())) throw Error('SEARCH_ADAPTER_PATH_INVALID');
  const temporary = file + '.' + crypto.randomUUID() + '.tmp';
  try {
    fs.writeFileSync(temporary, fs.readFileSync(path.join(__dirname, 'desktop-search-plugin.mjs')), {flag: 'wx'});
    fs.renameSync(temporary, file);
  } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  return [{id: 'web-search-deepseek', disabled: true},
    {id: 'web', config: {searchProvider: 'megmeet-managed-search', fetchProvider: 'megmeet-managed-search'}},
    {insert: [{id: 'megmeet-desktop-search', name: pathToFileURL(file).href, config: {endpoint, token}}]}];
}

function requestContext(req, res, context) {
  const abort = new AbortController();
  const cancel = () => abort.abort();
  const close = () => { if (!res.writableFinished) cancel(); };
  req.once('aborted', cancel); res.once('close', close);
  return {context: {...context, signal: AbortSignal.any([context.signal, abort.signal].filter(Boolean))},
    dispose() { req.off('aborted', cancel); res.off('close', close); }};
}

function createDesktopNativeWeb({toolkit, token, context, alive}) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token || '') || typeof alive !== 'function') throw Error('SEARCH_HOST_INVALID');
  const expected = Buffer.from('Bearer ' + token);
  return async function handle(req, res) {
    let scope;
    const reply = (status, value) => {
      if (!res.headersSent && !res.destroyed) {
        res.writeHead(status, {'Content-Type':'application/json', 'Cache-Control':'no-store'});
        res.end(JSON.stringify(value));
      }
    };
    try {
      const actual = Buffer.from(String(req.headers.authorization || ''));
      if (req.method !== 'POST' || req.url !== '/__megmeet/native-web' || req.headers.origin
          || req.headers['sec-fetch-site'] || actual.length !== expected.length
          || !crypto.timingSafeEqual(actual, expected)) { reply(403,{detail:'WEB_AUTH_REQUIRED'}); return; }
      if (!alive()) { reply(401,{detail:'WEB_AUTH_REQUIRED'}); return; }
      scope = requestContext(req, res, context);
      const chunks = []; let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > 8192) { reply(413,{detail:'WEB_ARGUMENTS_INVALID'}); return; }
        chunks.push(chunk);
      }
      let body; try { body = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
      catch { reply(400,{detail:'WEB_ARGUMENTS_INVALID'}); return; }
      scope.context.signal.throwIfAborted();
      if (!alive()) { reply(401,{detail:'WEB_AUTH_REQUIRED'}); return; }
      const value = await toolkit.nativeWeb(body,scope.context);
      scope.context.signal.throwIfAborted();
      if (!alive()) { reply(401,{detail:'WEB_AUTH_REQUIRED'}); return; }
      reply(200,value);
    } catch (error) {
      const code = typeof error?.code==='string' && /^[A-Z_]{1,100}$/.test(error.code) ? error.code : 'WEB_UNAVAILABLE';
      reply(Number.isInteger(error?.status) && error.status>=400 && error.status<=599 ? error.status : 502,{detail:code});
    } finally { scope?.dispose(); }
  };
}
module.exports = {desktopSearchPatch, requestContext, createDesktopNativeWeb};
