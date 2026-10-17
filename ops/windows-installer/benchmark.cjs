'use strict';
// Bounded real NSIS rehearsal. Every executable, appId, install and sample file
// is created under this run's unique temporary root; no employee install used.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const { build, Platform, Arch } = require('electron-builder');
const { writeInstallerInclude } = require('./path-policy.cjs');
const { createUpdater, launchInstaller } = require('../../desktop-local/updater.cjs');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const digest = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
async function main() {
  if (process.platform !== 'win32') throw Error('Windows rehearsal only');
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-install-speed-'));
  const id = path.basename(base).toLowerCase();
  const result = { scope: 'synthetic NSIS apps only; no employee data or company calls', root: base, samples: [], checks: [] };
  const project = path.join(base, 'project'); fs.mkdirSync(project);
  const fixture = path.join(base, 'fixture'); fs.mkdirSync(fixture);
  fs.mkdirSync(path.join(fixture, 'resources'));
  const exeName = 'MEGMEET-Installer-Probe.exe';
  fs.copyFileSync(path.join(process.env.SystemRoot, 'System32', 'ping.exe'), path.join(fixture, exeName));
  const manifest = [];
  for (let n = 0; n < 1200; n++) {
    const relative = `resources/packages/package-${n % 180}/module-${n}.js`, target = path.join(fixture, relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, '// synthetic small-file fixture\n' + JSON.stringify({ n, source: 'fixture '.repeat(256), noise: crypto.randomBytes(160).toString('hex') }));
    manifest.push([relative, digest(target)]);
  }
  for (let n = 0; n < 8; n++) {
    const relative = `resources/chunk-${n}.bin`, target = path.join(fixture, relative);
    fs.writeFileSync(target, crypto.randomBytes(1024 * 1024)); manifest.push([relative, digest(target)]);
  }
  const data = path.join(base, 'synthetic employee data'); fs.mkdirSync(data);
  fs.writeFileSync(path.join(data, 'history.json'), 'PRESERVE SYNTHETIC HISTORY');
  const nsisBase = { oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true,
    packElevateHelper: false, runAfterFinish: false, deleteAppDataOnUninstall: false,
    createDesktopShortcut: false, createStartMenuShortcut: false,
    include: writeInstallerInclude(fixture, path.join(base, 'payload-policy.nsh')).include };
  async function make(mode, version) {
    fs.writeFileSync(path.join(fixture, 'release.txt'), version);
    fs.writeFileSync(path.join(project, 'package.json'), JSON.stringify({ name: id + '-' + mode, version, description: 'Synthetic bounded benchmark', author: 'MEGMEET', main: 'unused.cjs' }));
    const begin = performance.now();
    const files = await build({ projectDir: project, prepackaged: fixture, publish: 'never', targets: Platform.WINDOWS.createTarget('nsis', Arch.x64), config: {
      appId: 'com.megmeet.installer.benchmark.' + id + '.' + mode, productName: 'MEGMEET Installer Probe ' + id + '-' + mode, executableName: 'MEGMEET-Installer-Probe',
      electronVersion: '44.4.1', directories: { output: path.join(base, 'out-' + mode + '-' + version) }, npmRebuild: false, publish: null,
      artifactName: 'fixture-${version}.exe', compression: 'normal', win: { signAndEditExecutable: false, icon: path.resolve(__dirname, '../../desktop-local/assets/app-icon.ico') },
      nsis: { ...nsisBase, ...(mode === 'zip' ? { differentialPackage: false, useZip: true } : {}) },
    } });
    const file = files.find(p => p.endsWith('.exe'));
    const sample = { mode, version, buildMs: Math.round(performance.now() - begin), bytes: fs.statSync(file).size };
    result.samples.push(sample); return { file, sample };
  }
  function verify(install, version) {
    if (fs.readFileSync(path.join(install, 'release.txt'), 'utf8') !== version) throw Error('Wrong installed version');
    for (const [relative, sha] of manifest) if (digest(path.join(install, relative)) !== sha) throw Error('Installed fixture differs: ' + relative);
    if (fs.readFileSync(path.join(data, 'history.json'), 'utf8') !== 'PRESERVE SYNTHETIC HISTORY') throw Error('Data changed');
  }
  function run(file, args, timeout = 180000) {
    const started = performance.now();
    const child = spawnSync(file, args, { windowsHide: true, windowsVerbatimArguments: true, timeout });
    if (child.error || child.status !== 0) throw Error('Synthetic setup failed: ' + child.status + ' ' + child.error);
    return Math.round(performance.now() - started);
  }
  const outside = path.join(base, 'outside'); fs.mkdirSync(outside);
  fs.copyFileSync(path.join(fixture, exeName), path.join(outside, exeName));
  const runningOutside = spawn(path.join(outside, exeName), ['-t', '127.0.0.1'], { windowsHide: true, stdio: 'ignore' });
  try {
    for (const mode of ['7z', 'zip']) {
      const { file, sample } = await make(mode, '0.0.1');
      const install = path.join(base, 'chosen folder 汉字', mode);
      sample.installMs = run(file, ['/S', '/currentuser', '/D=' + install]); verify(install, '0.0.1');
      console.log('MEASURED_INSTALL:', JSON.stringify(sample));
      result.checks.push(mode + '_FULL_FILE_HASHES_AND_SAME_NAME_OTHER_PATH_ALLOWED');
      if (mode === 'zip') {
        const { file: nextFile, sample: upgrade } = await make(mode, '0.0.2');
        const ownRunning = spawn(path.join(install, exeName), ['-t', '127.0.0.1'], { windowsHide: true, stdio: 'ignore' });
        await sleep(200);
        const refused = spawnSync(nextFile, ['/S', '--updated', '/currentuser', '/D=' + install], { windowsHide: true, windowsVerbatimArguments: true, timeout: 45000 });
        if (refused.error || refused.status !== 2 || ownRunning.exitCode !== null) throw Error('Running target was not preserved/refused');
        verify(install, '0.0.1'); ownRunning.kill(); await sleep(300);
        result.checks.push('RUNNING_TARGET_REFUSED_WITHOUT_KILL_OR_OVERWRITE');
        fs.writeFileSync(path.join(install, 'Uninstall MEGMEET-AI.exe'), 'synthetic installed marker');
        const keys = crypto.generateKeyPairSync('ed25519'), bytes = fs.readFileSync(nextFile);
        const payload = JSON.stringify({ product: 'com.megmeet.ai', channel: 'company', platform: 'win32', arch: 'x64', version: '0.0.2', sequence: 1,
          issuedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 86400000).toISOString(), artifact: {
            file: 'MEGMEET-AI-Setup-0.0.2-x64.exe', bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') } });
        const envelope = JSON.stringify({ schema: 1, keyId: 'synthetic', payload, signature: crypto.sign(null, Buffer.from(payload), keys.privateKey).toString('base64') });
        const updater = createUpdater({ currentVersion: '0.0.1', directory: path.join(base, 'cache'), policy: { keyId: 'synthetic', publicKey: keys.publicKey.export({ type: 'spki', format: 'pem' }) },
          getFeed: async () => envelope, download: async (_item, target) => fs.promises.copyFile(nextFile, target), launch: (target) => launchInstaller(target, install) });
        await updater.checkNow(); const started = performance.now();
        if (!await updater.installAfterQuit({ applicationPath: install })) throw Error('Signed handoff failed');
        let done = false;
        for (let n = 0; n < 360; n++) {
          // release.txt may be extracted before all resources. Do not mistake
          // the first visible new file for completion of the whole payload.
          try { if (fs.readFileSync(path.join(install, 'release.txt'), 'utf8') === '0.0.2' &&
            manifest.every(([relative]) => fs.existsSync(path.join(install, relative))) &&
            fs.existsSync(path.join(install, 'Uninstall MEGMEET-Installer-Probe.exe'))) { done = true; break; } } catch {}
          await sleep(500);
        }
        if (!done) throw Error('Upgrade did not complete');
        await sleep(1500); verify(install, '0.0.2'); upgrade.installMs = Math.round(performance.now() - started);
        result.checks.push('SIGNED_UPDATER_REUSES_CUSTOM_UNICODE_SPACED_PATH_AND_PRESERVES_DATA');
      }
      run(path.join(install, 'Uninstall MEGMEET-Installer-Probe.exe'), ['/S', '/KEEP_APP_DATA', '_?=' + install]);
      if (fs.existsSync(path.join(install, 'release.txt'))) throw Error('Uninstall incomplete');
    }
  } finally { runningOutside.kill(); }
  result.checks.push('OWNED_UNINSTALLS_PRESERVE_SYNTHETIC_EMPLOYEE_DATA');
  result.filesVerifiedPerInstall = manifest.length;
  result.completedAt = new Date().toISOString();
  const evidence = process.argv[2] || path.resolve(__dirname, '../../desktop-local/evidence/installer-optimization-rehearsal.json');
  fs.mkdirSync(path.dirname(evidence), { recursive: true }); fs.writeFileSync(evidence, JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
}
main().catch(e => { console.error(e); process.exitCode = 1; });

