'use strict';
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), assert = require('node:assert/strict');
const desktop = path.resolve(__dirname, '../../desktop-local');
const { startHost } = require(path.join(desktop, 'runtime-host.cjs'));
const { signIn, signOut, GATEWAY } = require(path.join(desktop, 'auth.cjs'));
const { fixtureBrowser } = require(path.join(desktop, 'fixture-browser.cjs'));
async function main() {
  assert.equal(GATEWAY, 'http://127.0.0.1:28090', 'This probe is local synthetic only');
  const stateRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-community-outer-'));
  let host, auth;
  const result = { scope: 'real startHost and local synthetic IAM/broker; isolated account files; no company network', stateRoot, checks: [] };
  try {
    auth = await signIn((url, options) => fixtureBrowser(url, 'a', options));
    host = await startHost({ auth, stateRoot, log: process.stdout });
    const cookie = host.browserCookie.name + '=' + host.browserCookie.value;
    for (const name of ['file-change-viewer', 'web-style', 'widget-kit', 'token-meter']) {
      const endpoint = host.origin + '/ext/dshp-' + name + '/state';
      assert.equal((await fetch(endpoint)).status, 401, name + ' anonymous');
      assert.equal((await fetch(endpoint, { headers: { Cookie: cookie, Origin: 'https://untrusted.invalid' } })).status, 403, name + ' foreign origin');
      const accepted = await fetch(endpoint, { headers: { Cookie: cookie, Origin: host.origin } });
      assert.equal(accepted.status, 200, name + ' authenticated');
      const value = await accepted.json(); assert.notEqual(value.ok, false);
      result.checks.push(name.toUpperCase().replaceAll('-', '_') + '_OUTER_AUTHENTICATED_AND_ANONYMOUS_FOREIGN_ORIGIN_DENIED');
    }
    result.complete = true; result.completedAt = new Date().toISOString();
    fs.writeFileSync(path.join(desktop, 'evidence/community-plugins-desktop-probe.json'), JSON.stringify(result, null, 2) + '\n');
    console.log(JSON.stringify(result, null, 2));
  } finally { if (host) await host.stop(); if (auth) await signOut(auth).catch(() => {}); }
}
main().catch(e => { console.error('COMMUNITY_DESKTOP_PROBE:', e.message); process.exitCode = 1; });
