'use strict';
const fs = require('node:fs'), path = require('node:path');
const { build, Platform, Arch } = require('electron-builder');
const { writeInstallerInclude } = require('./path-policy.cjs');
(async () => {
  const root = path.resolve(__dirname, '../..'), desktop = path.join(root, 'desktop-local');
  const version = JSON.parse(fs.readFileSync(path.join(desktop, 'package.json'))).version;
  const prepackaged = path.join(desktop, 'dist', version + '-company', 'MEGMEET AI-win32-x64');
  const audit = JSON.parse(fs.readFileSync(path.join(desktop, 'evidence', `packaged-${version}-company-audit.json`)));
  if (audit.version !== version || audit.packagedRuntime !== 'PASS') throw Error('Candidate must pass its packaged audit');
  const output = path.join(root, 'releases', `installer-${version}`);
  if (fs.existsSync(output)) throw Error('Installer output already exists; releases are immutable');
  fs.mkdirSync(output, { recursive: true });
  const policy = writeInstallerInclude(prepackaged, path.join(output, 'payload-path-policy.nsh'));
  console.log('INSTALLER_PATH_POLICY:', JSON.stringify(policy));
  const files = await build({ projectDir: desktop, prepackaged, targets: Platform.WINDOWS.createTarget('nsis', Arch.x64),
    publish: 'never', config: {
      appId: 'com.megmeet.ai', productName: '麦米办公助手', executableName: 'MEGMEET-AI',
      electronVersion: '44.4.1', directories: { output, buildResources: path.join(desktop, 'assets') },
      artifactName: 'MEGMEET-AI-Setup-${version}-${arch}.${ext}',
      win: { icon: path.join(desktop, 'assets/app-icon.ico'), signAndEditExecutable: false, target: [{ target: 'nsis', arch: ['x64'] }] },
      nsis: { oneClick: false, perMachine: false, allowElevation: false, allowToChangeInstallationDirectory: true,
        packElevateHelper: false, runAfterFinish: true, differentialPackage: false, useZip: true,
        deleteAppDataOnUninstall: false, createDesktopShortcut: 'always', createStartMenuShortcut: true,
        shortcutName: '麦米办公助手', uninstallDisplayName: '麦米办公助手', include: policy.include,
        installerLanguages: ['zh_CN', 'en_US'], language: '2052' },
      compression: 'normal', npmRebuild: false, publish: null,
    } });
  for (const file of files) console.log('INSTALLER_ARTIFACT: ' + file);
})().catch(error => { console.error(error); process.exitCode = 1; });
