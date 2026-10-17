'use strict';
// The office component has its own protocol and manifest, independent of DSH.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');
const sha = data => crypto.createHash('sha256').update(data).digest('hex');
function verifyRemote(runtimeRoot, { full = false, policy = require('./office-policy.json') } = {}) {
  const root = path.join(runtimeRoot, 'office-remote');
  const bytes = fs.readFileSync(path.join(root, 'office-remote-manifest.json'));
  if (!policy.remote || sha(bytes) !== policy.remote.manifestSha256) throw Error('远程办公组件清单不匹配，请重新安装软件。');
  const manifest = JSON.parse(bytes);
  if (manifest.protocol !== 'megmeet-office-remote-v1' || manifest.version !== policy.remote.version) throw Error('远程办公组件版本不兼容。');
  if (fs.existsSync(path.join(runtimeRoot, 'office')) || fs.readdirSync(path.join(runtimeRoot, 'node_modules/@deepseek-ai')).some(n => /^libreoffice-kit(?:-|$)/.test(n))) throw Error('公司轻量候选仍包含大型办公组件。');
  const required = ['office-remote/index.mjs', 'office-remote/adapter.mjs', 'office-remote/transport.mjs',
    'office-remote/preview-converter.mjs', 'desktop-python/python.exe', 'desktop-python/python313.dll',
    'desktop-python/python313._pth', 'node_modules/@deepseek-ai/dsh-office-to-pdf/lib/index.js'];
  for (const name of full ? Object.keys(manifest.files) : required) {
    if (!name || name.includes('\\') || name.startsWith('/') || name.split('/').some(v => ['', '.', '..'].includes(v))) throw Error('远程办公组件路径异常。');
    const expected = manifest.files[name], file = path.join(runtimeRoot, ...name.split('/'));
    if (!expected || fs.lstatSync(file).isSymbolicLink()) throw Error('远程办公组件文件缺失。');
    const data = fs.readFileSync(file);
    if (data.length !== expected.bytes || sha(data) !== expected.sha256) throw Error('远程办公组件校验失败：' + name);
  }
  return { root, manifest };
}
function officePatch(runtimeRoot, { full = false, policy = require('./office-policy.json'), mode = 'local', endpoint, token } = {}) {
  if (mode === 'remote') {
    const { root } = verifyRemote(runtimeRoot, { full, policy });
    if (typeof endpoint !== 'string' || !/^http:\/\/127\.0\.0\.1:\d+\/__megmeet\/office$/.test(endpoint)
        || !/^[A-Za-z0-9_-]{32,128}$/.test(token || '')) throw Error('远程办公连接尚未准备。');
    return { insert: [{ id: 'megmeet-office', name: pathToFileURL(path.join(root, 'index.mjs')).href,
      config: { endpoint, token, timeoutMs: 240000 } }] };
  }
  if (mode !== 'local') throw Error('办公模式无效。');
  const root = path.join(runtimeRoot, 'office');
  const manifestBytes = fs.readFileSync(path.join(root, 'office-manifest.json'));
  if (sha(manifestBytes) !== policy.manifestSha256) throw Error('办公组件清单不匹配，请重新安装软件。');
  const manifest = JSON.parse(manifestBytes);
  if (manifest.protocol !== 'megmeet-office-v1' || manifest.version !== policy.version) throw Error('办公组件版本不兼容。');
  const required = ['engine.py', 'py/python.exe', 'py/python313.dll', 'ocr-config.json',
    'adapter/index.mjs', 'adapter/adapter.mjs', 'adapter/transport.mjs', 'libreoffice/program/soffice.exe'];
  for (const name of full ? Object.keys(manifest.files) : required) {
    if (!name || name.includes('\\') || name.startsWith('/') || name.split('/').some(x => ['', '..', '.'].includes(x))) throw Error('办公组件文件路径异常。');
    const expected = manifest.files[name];
    const file = path.join(root, ...name.split('/'));
    if (!expected || fs.lstatSync(file).isSymbolicLink()) throw Error('办公组件文件缺失。');
    const bytes = fs.readFileSync(file);
    if (bytes.length !== expected.bytes || sha(bytes) !== expected.sha256) throw Error('办公组件文件校验失败：' + name);
  }
  return { insert: [{ id: 'megmeet-office', name: pathToFileURL(path.join(root, 'adapter/index.mjs')).href,
    config: { engineRoot: root, pythonExecutable: path.join(root, 'py/python.exe'), timeoutMs: 120000 } }] };
}
async function verifyOfficeFull(runtimeRoot, { mode = 'local', policy = require('./office-policy.json') } = {}) {
  if (mode === 'remote') {
    const { manifest } = verifyRemote(runtimeRoot, { full: true, policy });
    return { version: manifest.version, protocol: manifest.protocol, files: Object.keys(manifest.files).length };
  }
  if (mode !== 'local') throw Error('办公模式无效。');
  officePatch(runtimeRoot);
  const root = path.join(runtimeRoot, 'office');
  const manifest = JSON.parse(await fs.promises.readFile(path.join(root, 'office-manifest.json'), 'utf8'));
  const queue = Object.entries(manifest.files);
  await Promise.all(Array.from({length:12}, async () => {
    for (;;) {
      const entry = queue.pop(); if (!entry) break;
      const [name, expected] = entry;
      if (!name || name.includes('\\') || name.startsWith('/') || name.split('/').some(x => ['', '..', '.'].includes(x))) throw Error('Office path invalid');
      const file = path.join(root, ...name.split('/'));
      if ((await fs.promises.lstat(file)).isSymbolicLink()) throw Error('Office link refused');
      const bytes = await fs.promises.readFile(file);
      if (bytes.length !== expected.bytes || sha(bytes) !== expected.sha256) throw Error('Office file differs: ' + name);
    }
  }));
  return {version:manifest.version, protocol:manifest.protocol, files:Object.keys(manifest.files).length};
}
function desktopPython(runtimeRoot, { mode = 'local', policy = require('./office-policy.json') } = {}) {
  if (mode === 'remote') { verifyRemote(runtimeRoot, { policy }); return path.join(runtimeRoot, 'desktop-python/python.exe'); }
  if (mode !== 'local') throw Error('办公模式无效。');
  return path.join(runtimeRoot, 'office/py/python.exe');
}
module.exports = { officePatch, verifyOfficeFull, verifyRemote, desktopPython };
