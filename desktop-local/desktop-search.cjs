'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {pathToFileURL} = require('node:url');

// Native Node cannot import modules inside Electron app.asar. Materialize only
// this shipped, self-contained adapter in the already-owned account directory.
function desktopSearchPatch(home, endpoint, token) {
  const base = new URL(endpoint);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port
      || base.pathname !== '/__megmeet/tool-execute' || base.search || base.hash
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
    {id: 'web', config: {searchProvider: 'megmeet-managed-search'}},
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
module.exports = {desktopSearchPatch, requestContext};
