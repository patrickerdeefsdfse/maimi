'use strict';

const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const buildRequire = require('node:module').createRequire("D:\\\u9ea6\u683c\u7c73\u7279\\linux\\new-system\\desktop-local\\package.json");
const { packager } = buildRequire('@electron/packager');

(async () => {
  const root = path.resolve(__dirname, '..');
  const runtimeSource = path.resolve(process.env.MEGMEET_PACKAGE_RUNTIME || path.join(root, 'runtime'));
  const company = process.argv.includes('--company');
  const officeMode = company || fs.existsSync(path.join(runtimeSource, 'office-remote/office-remote-manifest.json')) ? 'remote' : 'local';
  const adapter = require('../dsh-adapter.cjs');
  adapter.validateRuntime(runtimeSource, { full: true });
  await require('../office-boundary.cjs').verifyOfficeFull(runtimeSource, { mode: officeMode });
  require('../toolkit-boundary.cjs').verifyToolkit(runtimeSource);
  const version = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8')).version;
  const appName = company ? 'MEGMEET AI' : 'MEGMEET AI Local';
  const configuration = { mode: company ? 'company' : 'local-development',
    gatewayOrigin: company ? 'https://platform-ai.megmeet.com' : 'http://127.0.0.1:28090' };
  const sourceFiles = ["account-lease.cjs","bootstrap.cjs","self-check.cjs","auth.cjs","backend.cjs","catalog.mjs","community-profile.mjs","deployment.cjs","desktop-search-plugin.mjs","desktop-search.cjs","device-session.cjs","diagnostic-log.cjs","dsh-adapter.cjs","enterprise-profile.mjs","entry.html","entry.js","gateway-dev.cjs","history-admission-rc2.mjs","history-migration.mjs","history-native.mjs","history-runtime-snapshot.mjs","main.cjs","managed-profile.mjs","office-boundary.cjs","office-bridge.cjs","office-policy.json","package.json","personal-cloud-bridge.cjs","policy.cjs","preload-native.cjs","preload-update.cjs","preload.cjs","process-tree.cjs","process-evidence-probe.cjs","model-boundary.cjs","model-policy-profile.cjs","enterprise-model-policy.mjs","project-artifact-proxy.cjs","remembered-store.cjs","runtime-host.cjs","shortcut-ipc.cjs","shortcut-keybindings.cjs","shortcut-keyboard.cjs","shortcut-protocol.cjs","toolkit-boundary.cjs","toolkit-policy.json","update-policy.json","update-window.cjs","update.html","update.js","updater.cjs"];
  if (!company) sourceFiles.push('fixture-browser.cjs');
  // Reuse the verified official archive when available; do not re-download it
  // just because @electron/get changed its URL/cache-key implementation.
  const archive = 'electron-v44.4.1-win32-x64.zip';
  const cacheRoot = path.join(process.env.LOCALAPPDATA || '', 'electron', 'Cache');
  let electronZipDir;
  if (fs.existsSync(cacheRoot)) {
    const checksums = JSON.parse(fs.readFileSync("D:\\\u9ea6\u683c\u7c73\u7279\\linux\\new-system\\desktop-local\\node_modules\\electron\\checksums.json", 'utf8'));
    for (const item of fs.readdirSync(cacheRoot, { withFileTypes: true })) {
      if (!item.isDirectory()) continue;
      const candidate = path.join(cacheRoot, item.name, archive);
      if (!fs.existsSync(candidate)) continue;
      const sha = crypto.createHash('sha256').update(fs.readFileSync(candidate)).digest('hex');
      if (sha !== checksums[archive]) throw Error('Cached Electron archive checksum differs');
      electronZipDir = path.dirname(candidate);
      break;
    }
  }
  if (!electronZipDir) throw Error('Pinned local Electron archive is required; this candidate build never downloads a new runtime');
  const paths = await packager({
    tmpdir: fs.mkdtempSync(path.join(require('node:os').tmpdir(), company ? 'megmeet-company-build-' : 'megmeet-local-build-')),
    dir: root, out: path.join("C:\\Users\\LI YUHAO\\.codex\\tmp\\dsh020-r41-desktop\\packages", version + (company ? '-company' : '')), name: appName,
    executableName: 'MEGMEET-AI', platform: 'win32', arch: 'x64',
    electronVersion: '44.4.1', appVersion: version,
    electronZipDir, icon: path.join(root, 'assets/app-icon.ico'),
    appBundleId: company ? 'com.megmeet.ai' : 'com.megmeet.ai.local', prune: true, asar: true,
    afterCopy: [async ({ buildPath }) => {
      // Freeze only owned application modules; never ship smoke login or local
      // development tooling in the company application archive.
      const allowed = new Set(["account-lease.cjs","bootstrap.cjs","self-check.cjs","auth.cjs","backend.cjs","catalog.mjs","community-profile.mjs","deployment.cjs","desktop-search-plugin.mjs","desktop-search.cjs","device-session.cjs","diagnostic-log.cjs","dsh-adapter.cjs","enterprise-profile.mjs","entry.html","entry.js","gateway-dev.cjs","history-admission-rc2.mjs","history-migration.mjs","history-native.mjs","history-runtime-snapshot.mjs","main.cjs","managed-profile.mjs","office-boundary.cjs","office-bridge.cjs","office-policy.json","package.json","personal-cloud-bridge.cjs","policy.cjs","preload-native.cjs","preload-update.cjs","preload.cjs","process-tree.cjs","process-evidence-probe.cjs","model-boundary.cjs","model-policy-profile.cjs","enterprise-model-policy.mjs","project-artifact-proxy.cjs","remembered-store.cjs","runtime-host.cjs","shortcut-ipc.cjs","shortcut-keybindings.cjs","shortcut-keyboard.cjs","shortcut-protocol.cjs","toolkit-boundary.cjs","toolkit-policy.json","update-policy.json","update-window.cjs","update.html","update.js","updater.cjs","deployment.json"]);
      if (!company) allowed.add('fixture-browser.cjs');
      for (const item of fs.readdirSync(buildPath, { withFileTypes: true })) {
        if (allowed.has(item.name) || item.isDirectory()) continue;
        fs.unlinkSync(path.join(buildPath, item.name));
      }
      fs.writeFileSync(path.join(buildPath, 'deployment.json'), JSON.stringify(configuration) + '\n');
    }],
    overwrite: false,
    extraResource: [path.join(root, 'assets/app-icon.ico')],
    ignore: [/^\/dist(?:-candidate)?($|\/)/, /^\/tests($|\/)/, /^\/scripts($|\/)/, /^\/evidence($|\/)/,
      /^\/node_modules($|\/)/, /^\/runtime($|\/)/, /\.log$/, /^\/package-lock\.json$/, /^\/artifact.*\.json$/],
    win32metadata: { CompanyName: 'MEGMEET', ProductName: appName,
      FileDescription: 'MEGMEET native DSH local desktop client' },
  });
  // A frozen runtime contains many small upstream files. The Windows copier
  // preserves every byte with bounded retries and parallel I/O; never /MIR or
  // a destructive cleanup of the destination. Release directories are new.
  for (const directory of paths) {
    const target = path.join(directory, 'resources/runtime');
    if (fs.existsSync(target)) throw Error('Runtime destination already exists');
    if (process.platform === 'win32') {
      const copy = spawnSync('robocopy.exe', [runtimeSource, target, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:1', '/W:1', '/MT:16', '/XJ', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'], {windowsHide:true,stdio:'pipe'});
      if (copy.error || copy.status === null || copy.status >= 8) throw Error('Packaged runtime copy failed');
    } else fs.cpSync(runtimeSource, target, {recursive:true,errorOnExist:true,force:false});
    adapter.validateRuntime(target, { full: true });
    await require('../office-boundary.cjs').verifyOfficeFull(target, { mode: officeMode });
    require('../toolkit-boundary.cjs').verifyToolkit(target);
  }
  fs.writeFileSync(path.join(root, company ? 'artifact-company.json' : 'artifact.json'), JSON.stringify({
    created: new Date().toISOString(), electron: '44.4.1', version, deployment: configuration,
    runtime: JSON.parse(fs.readFileSync(path.join(runtimeSource, 'runtime-manifest.json'), 'utf8')),
    release: JSON.parse(fs.readFileSync(path.join(runtimeSource, 'release-manifest.json'), 'utf8')),
    adaptation: JSON.parse(fs.readFileSync(path.join(runtimeSource, 'brand-adaptation.json'), 'utf8')),
    runtimeSource, officeMode, sourceFilesMatched: sourceFiles, packagedRuntimeVerified: true,
    binaries: paths.flatMap(directory => ['MEGMEET-AI.exe', 'resources/app.asar'].map(relative => {
      const file = path.join(directory, relative);
      return { path: file, bytes: fs.statSync(file).size,
        sha256: crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') };
    })),
    directories: paths, signed: false, localDevelopmentOnly: !company,
    companyDeploymentVerified: false,
  }, null, 2) + '\n');
  paths.forEach(item => console.log(item));
})().catch(error => { console.error(error); process.exitCode = 1; });
