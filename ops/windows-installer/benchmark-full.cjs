'use strict';
// Full shipped payload, disposable NSIS identity. No shipped artifact is edited.
const fs = require('node:fs'), path = require('node:path'), os = require('node:os'), crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { build, Platform, Arch } = require('electron-builder');
const { writeInstallerInclude } = require('./path-policy.cjs');
const sha = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
async function main() {
  if (process.platform !== 'win32') throw Error('Windows only');
  const zipOnly = process.argv.includes('--zip-only');
  const source = path.resolve(process.argv.slice(2).find(arg => !arg.startsWith('--')) || path.join(__dirname, '../../desktop-local/dist/0.5.0-company/MEGMEET AI-win32-x64'));
  if (!fs.existsSync(path.join(source, 'MEGMEET-AI.exe')) || !fs.existsSync(path.join(source, 'resources/runtime/runtime-manifest.json'))) throw Error('Full packaged application required');
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-full-installer-'));
  const id = path.basename(base).toLowerCase(), project = path.join(base, 'project'); fs.mkdirSync(project);
  fs.writeFileSync(path.join(project, 'package.json'), JSON.stringify({ name: id, version: '0.0.1', description: 'Full-payload bounded rehearsal', author: 'MEGMEET', main: 'unused.cjs' }));
  const evidence = path.resolve(__dirname, '../../desktop-local/evidence/installer-full-payload-' + (zipOnly ? 'zip-' : '') + 'benchmark.json');
  const result = { scope: 'actual packaged bytes in disposable installer identity; never launched app; no employee data/company network', root: base, source, manifest: [], samples: [], complete: false };
  const save = () => fs.writeFileSync(evidence, JSON.stringify(result, null, 2) + '\n');
  function inventory(directory, relative = '') {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      if (entry.isSymbolicLink()) throw Error('Unexpected packaged link');
      const rel = path.join(relative, entry.name), absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) inventory(absolute, rel);
      else {
        result.manifest.push({ path: rel, bytes: fs.statSync(absolute).size, sha256: sha(absolute) });
        if (result.manifest.length % 2000 === 0) console.log('INVENTORY_FILES:', result.manifest.length);
      }
    }
  }
  inventory(source); result.fileCount = result.manifest.length; result.totalBytes = result.manifest.reduce((s, v) => s + v.bytes, 0); save();
  const policy = writeInstallerInclude(source, path.join(base, 'payload-policy.nsh'));
  const data = path.join(base, 'synthetic-data'); fs.mkdirSync(data); fs.writeFileSync(path.join(data, 'history.json'), 'UNCHANGED');
  async function run(exe, args, phase) {
    const start = performance.now(), child = spawn(exe, args, { windowsHide: true, windowsVerbatimArguments: true, stdio: 'ignore' });
    const progress = setInterval(() => console.log('INSTALL_PROGRESS:', phase, Math.round((performance.now() - start) / 1000), 'seconds'), 30000);
    const timeout = setTimeout(() => child.kill(), 30 * 60000);
    const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('exit', resolve); }).finally(() => { clearInterval(progress); clearTimeout(timeout); });
    if (code !== 0) throw Error(phase + ' failed: ' + code);
    return Math.round(performance.now() - start);
  }
  for (const mode of (zipOnly ? ['zip'] : ['7z', 'zip'])) {
    console.log('FULL_BUILD_START:', mode); const start = performance.now();
    const files = await build({ projectDir: project, prepackaged: source, publish: 'never', targets: Platform.WINDOWS.createTarget('nsis', Arch.x64), config: {
      appId: 'com.megmeet.full-rehearsal.' + id + '.' + mode, productName: 'MEGMEET Full Rehearsal ' + id + '-' + mode, executableName: 'MEGMEET-AI',
      electronVersion: '44.4.1', npmRebuild: false, publish: null, compression: 'normal', directories: { output: path.join(base, 'out-' + mode) },
      artifactName: 'full-${version}.exe', win: { signAndEditExecutable: false, icon: path.resolve(__dirname, '../../desktop-local/assets/app-icon.ico') },
      nsis: { oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true, packElevateHelper: false,
        runAfterFinish: false, deleteAppDataOnUninstall: false, createDesktopShortcut: false, createStartMenuShortcut: false,
        include: policy.include, ...(mode === 'zip' ? { differentialPackage: false, useZip: true } : {}) },
    } });
    const file = files.find(f => f.endsWith('.exe')), sample = { mode, buildMs: Math.round(performance.now() - start), installerBytes: fs.statSync(file).size };
    result.samples.push(sample); save();
    const installBase = fs.mkdtempSync(path.resolve(__dirname, '../../../output/if-'));
    const install = path.join(installBase, mode);
    if (install.length > policy.maxInstallDirectoryChars) throw Error('Synthetic install path exceeds actual payload limit');
    sample.installMs = await run(file, ['/S', '/currentuser', '/D=' + install], mode + '-install'); save();
    console.log('FULL_INSTALL_MEASURED:', JSON.stringify(sample));
    let verified = 0;
    for (const item of result.manifest) {
      const target = path.join(install, item.path);
      if (fs.statSync(target).size !== item.bytes || sha(target) !== item.sha256) throw Error('Installed payload differs: ' + item.path);
      if (++verified % 2000 === 0) console.log('VERIFIED_FILES:', mode, verified);
    }
    sample.everySourceFileVerified = verified; save();
    sample.uninstallMs = await run(path.join(install, 'Uninstall MEGMEET-AI.exe'), ['/S', '/KEEP_APP_DATA', '_?=' + install], mode + '-uninstall');
    if (fs.existsSync(path.join(install, 'resources/app.asar')) || fs.readFileSync(path.join(data, 'history.json'), 'utf8') !== 'UNCHANGED') throw Error('Uninstall or data preservation failed');
    sample.uninstallAndDataPreservation = 'PASS'; save();
  }
  result.complete = true; result.completedAt = new Date().toISOString(); save();
  console.log('FULL_PAYLOAD_BENCHMARK: PASS', JSON.stringify(result.samples));
}
main().catch(e => { console.error(e); process.exitCode = 1; });
