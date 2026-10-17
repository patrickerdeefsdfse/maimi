import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { stageCommunity } from './community-plugins.mjs';
const here = path.dirname(fileURLToPath(import.meta.url));
const source = path.resolve(process.env.MEGMEET_CANDIDATE_RUNTIME || path.resolve(here, '../../desktop-local/runtime'));
const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-community-probe-'));
const runtime = path.join(directory, 'runtime'), modules = path.join(runtime, 'node_modules');
fs.mkdirSync(modules, { recursive: true });
for (const entry of fs.readdirSync(path.join(source, 'node_modules'), { withFileTypes: true })) {
  if (!entry.isDirectory() || entry.name === '@dshp' || entry.name === '.bin') continue;
  fs.symlinkSync(path.join(source, 'node_modules', entry.name), path.join(modules, entry.name), process.platform === 'win32' ? 'junction' : 'dir');
}
const staged = stageCommunity(runtime);
// Absolute names ensure the loader resolves this isolated candidate package,
// irrespective of the native CLI's real path through a read-only junction.
const patch = JSON.parse(fs.readFileSync(staged.patchPath, 'utf8'));
if (!process.argv.includes('--native-names')) {
  for (const item of patch[0].insert) item.name = path.join(modules, item.name, 'megmeet-guarded-host.mjs').replaceAll('\\', '/');
}
const probePatch = path.join(directory, 'probe.patch.json'); fs.writeFileSync(probePatch, JSON.stringify(patch));
const port = await new Promise((resolve, reject) => { const server = net.createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const p = server.address().port; server.close(() => resolve(p)); }); });
const base = 'http://127.0.0.1:' + port;
for (const name of ['home','dsh','appdata','localappdata']) fs.mkdirSync(path.join(directory, name));
const env = { PATH: process.env.PATH, SystemRoot: process.env.SystemRoot, WINDIR: process.env.WINDIR,
  TEMP: os.tmpdir(), TMP: os.tmpdir(), HOME: path.join(directory, 'home'), USERPROFILE: path.join(directory, 'home'),
  APPDATA: path.join(directory, 'appdata'), LOCALAPPDATA: path.join(directory, 'localappdata'),
  DSH_HOME: path.join(directory, 'dsh'), DSH_TELEMETRY_DISABLED: '1' };
const child = spawn(path.join(source, 'bin', process.platform === 'win32' ? 'node.exe' : 'node'),
  ['--expose-internals', '--import', pathToFileURL(path.join(here, 'community-plugins-network-guard.mjs')).href,
    path.join(source, 'node_modules/@deepseek-ai/dsh/lib/bin.js'), '--profile', 'web', '--patch', probePatch,
    '--host', '127.0.0.1', '--port', String(port), '--no-open'], { cwd: runtime, env, windowsHide: true, stdio: ['ignore','pipe','pipe'] });
let output = ''; child.stdout.on('data', b => output = (output + b).slice(-65536)); child.stderr.on('data', b => output = (output + b).slice(-65536));
let cookie = '';
const checks = [];
const rawAnonymousStatuses = {};
try {
  for (let i = 0; i < 180; i++) {
    if (child.exitCode !== null) throw Error('Native probe exited before ready');
    const token = output.match(/\?token=([A-Za-z0-9_-]+)/)?.[1];
    if (token) {
      const r = await fetch(base + '/?token=' + token, { redirect: 'manual' });
      cookie = r.headers.getSetCookie().map(s => s.split(';')[0]).join('; ');
      if (cookie) break;
    }
    await new Promise(r => setTimeout(r, 500));
  }
  if (!cookie) throw Error('Native local authentication bootstrap timed out');
  const page = await fetch(base + '/', { headers: { Cookie: cookie } });
  if (page.status !== 200) throw Error('Native index failed');
  const html = await page.text();
  fs.writeFileSync(path.join(directory, 'index.synthetic.html'), html);
  checks.push('NATIVE_INDEX_AND_PRIVATE_COOKIE');
  for (const name of ['file-change-viewer','web-style','widget-kit','token-meter']) {
    const clientUrl = html.match(new RegExp('"id":"@dshp/' + name + '","url":"([^"\\n]+)"'))?.[1];
    if (!clientUrl || !clientUrl.startsWith('/plugins/')) throw Error(name + ' client module was not discovered');
    const client = await fetch(base + clientUrl, { headers: { Cookie: cookie } });
    if (client.status !== 200 || !(await client.text()).includes('@dshp/' + name)) throw Error(name + ' client module could not be served');
    const route = '/ext/dshp-' + name + '/state';
    const r = await fetch(base + route, { headers: { Cookie: cookie } });
    const body = await r.text();
    if (r.status !== 200 || !body.startsWith('{')) throw Error(name + ' state endpoint failed: ' + r.status);
    const state = JSON.parse(body);
    if (state.ok === false) throw Error(name + ' state reports failure');
    const anonymous = await fetch(base + route);
    rawAnonymousStatuses[name] = anonymous.status;
    if (anonymous.status !== 401) throw Error(name + ' anonymous route was not denied');
    const foreign = await fetch(base + route, { headers: { Cookie: cookie, Origin: 'https://untrusted.invalid' } });
    if (foreign.status !== 403) throw Error(name + ' foreign Origin was not denied');
    checks.push(name.toUpperCase().replaceAll('-', '_') + '_HOST_STATE');
    checks.push(name.toUpperCase().replaceAll('-', '_') + '_CLIENT_DISCOVERY_AND_ASSET');
  }
  if (/COMMUNITY_PROBE_EXTERNAL_NETWORK_DENIED/.test(output)) throw Error('Plugin attempted network during default load');
  const evidence = { completedAt: new Date().toISOString(), scope: 'temporary DSH_HOME; synthetic data; outbound non-loopback TCP denied',
    directory, dsh: '0.1.6-alpha.2', staged, checks, rawAnonymousStatuses,
    rawExtensionAuthentication: 'NATIVE_CONNECTION_AUTH_GUARDED_ENTRY', browserRendering: 'NOT_TESTED', modelCall: 'NOT_TESTED' };
  const file = path.resolve(process.env.MEGMEET_COMMUNITY_EVIDENCE || path.resolve(here, '../../desktop-local/evidence/community-plugins-host-probe.json'));
  fs.writeFileSync(file, JSON.stringify(evidence, null, 2) + '\n');
  console.log(JSON.stringify(evidence, null, 2));
} catch (error) {
  console.error('COMMUNITY_PROBE_FAILURE:', error.message);
  console.error(output.replace(/token=[A-Za-z0-9_-]+/g, 'token=[redacted]').split('\n').slice(-28).join('\n'));
  process.exitCode = 1;
} finally {
  child.kill();
  await new Promise(resolve => { if (child.exitCode !== null) resolve(); else child.once('exit', resolve); });
}
