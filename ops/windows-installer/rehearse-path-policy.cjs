'use strict';
// Compile an isolated tiny app with the deepest path from the final release,
// exercising the real NSIS macros without touching any employee installation.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { build, Platform, Arch } = require('electron-builder');
const { measurePayload, writeInstallerInclude } = require('./path-policy.cjs');
(async () => {
  const desktopVersion = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../desktop-local/package.json'))).version;
  const candidate = path.resolve(__dirname, `../../desktop-local/dist/${desktopVersion}-company/MEGMEET AI-win32-x64`);
  const finalPolicy = measurePayload(candidate);
  const base = fs.mkdtempSync(path.resolve(__dirname, '../../../output/ip-'));
  const project = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-path-nsis-'));
  const fixture = path.join(base, 'payload'); fs.mkdirSync(fixture);
  const sourceFile = path.join(fixture, ...finalPolicy.longestRelativePath.split('\\'));
  fs.mkdirSync(path.dirname(sourceFile), { recursive: true }); fs.writeFileSync(sourceFile, 'deep path fixture');
  fs.copyFileSync(path.join(process.env.SystemRoot, 'System32/ping.exe'), path.join(fixture, 'MEGMEET-Path-Probe.exe'));
  const policy = writeInstallerInclude(fixture, path.join(base, 'policy.nsh'));
  if (policy.maxInstallDirectoryChars !== finalPolicy.maxInstallDirectoryChars) throw Error('Fixture policy mismatch');
  const id = 'com.megmeet.path-probe.' + crypto.randomBytes(5).toString('hex');
  async function make(version) {
    fs.writeFileSync(path.join(project, 'package.json'), JSON.stringify({ name: 'path-probe', version, main: 'unused.cjs', author: 'MEGMEET', description: 'Isolated path test' }));
    fs.writeFileSync(path.join(fixture, 'version.txt'), version);
    const files = await build({ projectDir: project, prepackaged: fixture, publish: 'never', targets: Platform.WINDOWS.createTarget('nsis', Arch.x64), config: {
      appId: id, productName: 'MEGMEET Path Probe', executableName: 'MEGMEET-Path-Probe', electronVersion: '44.4.1', npmRebuild: false, publish: null,
      compression: 'normal', directories: { output: path.join(base, version) }, artifactName: 'probe-${version}.exe',
      win: { signAndEditExecutable: false, icon: path.resolve(__dirname, '../../desktop-local/assets/app-icon.ico') },
      nsis: { oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true, packElevateHelper: false,
        runAfterFinish: false, differentialPackage: false, useZip: true, include: policy.include, createDesktopShortcut: false, createStartMenuShortcut: false, deleteAppDataOnUninstall: false },
    } }); return files.find(file => file.endsWith('.exe'));
  }
  function run(exe, args, expected = 0) {
    const r = spawnSync(exe, args, { windowsHide: true, windowsVerbatimArguments: true, timeout: 90000 });
    if (r.error || r.status !== expected) throw Error('Unexpected NSIS result ' + r.status + ' ' + r.error);
  }
  const first = await make('0.0.1'), second = await make('0.0.2');
  const longTarget = path.join(base, '过长 ' + 'a'.repeat(policy.maxInstallDirectoryChars - base.length - 3));
  if (longTarget.length !== policy.maxInstallDirectoryChars + 1) throw Error('Long test length wrong');
  fs.mkdirSync(longTarget); fs.writeFileSync(path.join(longTarget, 'old-marker.txt'), 'OLD_UNCHANGED');
  for (const flags of [[], ['--updated']]) {
    run(second, ['/S', ...flags, '/currentuser', '/D=' + longTarget], 4);
    if (fs.readFileSync(path.join(longTarget, 'old-marker.txt'), 'utf8') !== 'OLD_UNCHANGED' || fs.readdirSync(longTarget).length !== 1) throw Error('Rejected path modified');
  }
  const target = path.join(base, '中文 ' + 'b'.repeat(policy.maxInstallDirectoryChars - base.length - 4));
  if (target.length !== policy.maxInstallDirectoryChars) throw Error('Boundary test length wrong');
  run(first, ['/S', '/currentuser', '/D=' + target]);
  if (fs.readFileSync(path.join(target, ...finalPolicy.longestRelativePath.split('\\')), 'utf8') !== 'deep path fixture') throw Error('Deepest path not installed');
  const data = path.join(base, 'synthetic-history.json'); fs.writeFileSync(data, 'PRESERVE');
  run(second, ['/S', '--updated', '/currentuser', '/D=' + target]);
  if (fs.readFileSync(path.join(target, 'version.txt'), 'utf8') !== '0.0.2' || fs.readFileSync(data, 'utf8') !== 'PRESERVE') throw Error('Upgrade failed');
  run(path.join(target, 'Uninstall MEGMEET-Path-Probe.exe'), ['/S', '/KEEP_APP_DATA', '_?=' + target]);
  if (fs.readFileSync(data, 'utf8') !== 'PRESERVE') throw Error('Data changed');
  const evidence = { scope: 'isolated real NSIS app; no employee data or company calls', desktopVersion, finalPolicy, base, target,
    checks: ['LONG_NEW_INSTALL_REFUSED_BEFORE_WRITES', 'LONG_UPDATED_INSTALL_REFUSED_BEFORE_WRITES', 'EXACT_LIMIT_UNICODE_SPACE_DEEPEST_FILE_PASS', 'SAME_DIRECTORY_UPGRADE_PASS', 'OWN_UNINSTALL_PRESERVES_DATA'], completedAt: new Date().toISOString() };
  fs.writeFileSync(path.resolve(__dirname, '../../desktop-local/evidence/installer-path-policy-rehearsal.json'), JSON.stringify(evidence, null, 2));
  console.log('PATH_POLICY_REAL_NSIS: PASS', JSON.stringify(evidence));
})().catch(error => { console.error(error); process.exitCode = 1; });
