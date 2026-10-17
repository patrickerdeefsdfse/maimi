'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), os = require('node:os');
const { measurePayload, writeInstallerInclude } = require('./path-policy.cjs');
test('path limit derives from every actual payload path using Windows UTF-16 lengths', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-path-policy-'));
  const rel = 'resources\\' + 'a'.repeat(60) + '\\' + '中文 空格'.repeat(20) + '.js';
  const file = path.join(root, ...rel.split('\\')); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, 'fixture');
  const policy = measurePayload(root);
  assert.equal(policy.longestRelativePath, rel);
  assert.equal(policy.maxInstallDirectoryChars + 1 + rel.length, 259);
  const include = path.join(os.tmpdir(), path.basename(root) + '.nsh');
  assert.equal(writeInstallerInclude(root, include).maxInstallDirectoryChars, policy.maxInstallDirectoryChars);
  assert.match(fs.readFileSync(include, 'utf8'), new RegExp('MEGMEET_MAX_INSTALL_DIR ' + policy.maxInstallDirectoryChars));
});
test('empty payload is rejected rather than falling back to an unmeasured limit', () => {
  assert.throws(() => measurePayload(fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-path-empty-'))), /Empty/);
});
test('silent path refusal precedes any process wait and generated installer does not bypass upstream path validation', () => {
  const source = fs.readFileSync(path.join(__dirname, 'installer.nsh'), 'utf8');
  const macro = source.slice(source.indexOf('!macro customCheckAppRunning'));
  assert.ok(macro.indexOf('SetErrorLevel 4') < macro.indexOf('megmeet_wait_for_exit:'));
  assert.match(source, /Function \.onVerifyInstDir/);
  assert.match(source, /StrContains.*APP_FILENAME/);
  assert.match(source, /MUI_DIRECTORYPAGE_TEXT_TOP/);
});
