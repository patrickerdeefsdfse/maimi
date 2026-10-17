import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { stageCommunity, verifyCommunitySource, COMMUNITY_DEFAULTS } from './community-plugins.mjs';
const here = path.dirname(fileURLToPath(import.meta.url));
const vendor = path.join(here, 'vendor-community');
test('fixed community release verifies every vendored artifact and client loader identity', () => {
  const m = verifyCommunitySource();
  assert.equal(m.plugins.length, 4);
  for (const p of m.plugins) {
    let registered;
    vm.runInNewContext(fs.readFileSync(path.join(vendor, p.directory, 'lib/client.js'), 'utf8'), {
      window: { __ModuleLoader__: { load: v => { registered = v; } } },
    }, { timeout: 1000 });
    assert.equal(registered.id, p.name); assert.equal(typeof registered.factory, 'function');
  }
});
test('stage is deterministic and rejects mixed DSH versions and changed candidates', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-community-stage-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const dsh = path.join(root, 'node_modules/@deepseek-ai/dsh'); fs.mkdirSync(dsh, { recursive: true });
  fs.writeFileSync(path.join(dsh, 'package.json'), '{"version":"0.1.5"}');
  assert.throws(() => stageCommunity(root), /tested DSH/);
  fs.writeFileSync(path.join(dsh, 'package.json'), '{"version":"0.1.6-alpha.2"}');
  const a = stageCommunity(root), b = stageCommunity(root);
  assert.equal(a.patchSha256, b.patchSha256);
  const patch = JSON.parse(fs.readFileSync(a.patchPath));
  assert.deepEqual(patch[0].insert.map(p => p.config), Object.values(COMMUNITY_DEFAULTS));
  fs.appendFileSync(path.join(root, 'node_modules/@dshp/widget-kit/lib/host.js'), '\n// tamper');
  assert.throws(() => stageCommunity(root), /unknown existing/);
});
test('tampered vendor binary is rejected even when metadata identity is unchanged', t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-community-tamper-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const manifest = verifyCommunitySource();
  fs.copyFileSync(path.join(vendor, 'manifest.json'), path.join(root, 'manifest.json'));
  for (const plugin of manifest.plugins) for (const relative of Object.keys(plugin.files)) {
    const target = path.join(root, plugin.directory, relative); fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.copyFileSync(path.join(vendor, plugin.directory, relative), target);
  }
  fs.appendFileSync(path.join(root, 'token-meter/lib/host.js'), '\n// changed');
  assert.throws(() => verifyCommunitySource(root), /integrity differs/);
});

test('guard blocks every route before upstream side effects and preserves native Context ownership', async t => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-community-guard-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.mkdirSync(path.join(root, 'lib'));
  fs.copyFileSync(path.join(here, 'community-plugins-guard.mjs'), path.join(root, 'guard.mjs'));
  fs.writeFileSync(path.join(root, 'package.json'), '{"type":"module"}');
  fs.writeFileSync(path.join(root, 'lib/host.js'), `
    export const inject = ['webServer'];
    export function apply(ctx) {
      ctx.effect(() => ctx.webServer.register({ kind:'exact',path:'/ext/dshp-test/state',handler(req,res) { res.end('upstream'); } }));
      ctx.inject(['settings'], child => child.webServer.register({ kind:'exact',path:'/ext/dshp-test/nested',handler(req,res) { res.end('nested'); } }));
      ctx.get('webServer').register({ kind:'exact',path:'/ext/dshp-test/get',handler(req,res) { res.end('upstream'); } });
    }
  `);
  const guarded = await import(pathToFileURL(path.join(root, 'guard.mjs')).href);
  assert.deepEqual(guarded.inject, ['webServer', 'connection']);
  let rejection = 401;
  const routes = [];
  const server = { register(route) { assert.equal(this, server); routes.push(route); } };
  const ctx = { webServer: server, connection: { requestRejection() { if (rejection === 'throw') throw Error('gone'); return rejection; } },
    effect(callback) { assert.equal(this, ctx); callback(); }, inject(deps, callback) { assert.equal(this, ctx); callback(ctx); } };
  guarded.apply(ctx, {});
  assert.equal(routes.length, 3);
  for (const route of routes) for (const input of [401, 403, 'throw', undefined]) {
    rejection = input;
    const result = {};
    route.handler({ method: 'GET' }, { writeHead(status) { result.status = status; }, end(body) { result.body = body; } });
    if (input === undefined) assert.match(result.body, /upstream|nested/);
    else { assert.equal(result.status, input === 'throw' ? 503 : input); assert.equal(result.body, 'community request denied'); }
  }
  assert.throws(() => guarded.apply({ webServer: server }, {}), /authenticated community/);
});
