import {verifyWindowsSecurity} from './verify-windows-security.mjs';
import { createRequire } from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
import {prepareInstallerTemplates} from './installer-templates.mjs';
import {prepareWizardSkin} from './wizard-skin.mjs';
import {useCompactPayload} from './compact-payload.mjs';
if (!process.env.MAIMI_WORK) throw Error('MAIMI_WORK required');
const buildRoot=path.resolve(process.env.MAIMI_WORK,'windows');
const packed = path.join(buildRoot,'win-unpacked');
const app = path.join(packed,'resources','app');
const release = path.join(buildRoot,'release');
const pins=JSON.parse(fs.readFileSync(new URL('../deps/pins.json',import.meta.url),'utf8'));
await verifyWindowsSecurity(packed,pins);
const require = createRequire(process.env.MAIMI_BUILD_TOOLS_PACKAGE??path.join(import.meta.dirname,'package.json'));
const { build, Platform, Arch } = require('electron-builder');
const builderRequire = createRequire(require.resolve('electron-builder/package.json'));
prepareInstallerTemplates(builderRequire, buildRoot);
const wizard=prepareWizardSkin(builderRequire,buildRoot,path.join(import.meta.dirname,'installer-ui.nsh'));
useCompactPayload(builderRequire, packed, Arch.x64);
const meta = JSON.parse(fs.readFileSync(path.join(app,'package.json')));
const dsh = JSON.parse(fs.readFileSync(path.join(app,'dsh','desktop-runtime.json')));
const releaseVersion = JSON.parse(fs.readFileSync(path.join(import.meta.dirname,'../deps/pins.json'))).dsh.release;
if (dsh.release.version !== releaseVersion) throw Error('wrong release');
const projectDir = path.join(buildRoot,'installer-project');
if(fs.existsSync(release))throw Error('Use a fresh release directory');
fs.mkdirSync(projectDir,{recursive:true});
fs.writeFileSync(path.join(projectDir,'package.json'), JSON.stringify({name:meta.name,version:meta.version,author:meta.author,description:meta.description,main:'lib/main.js'}));
// The updater reads this same URL from resources/app-update.yml in the installed program.
const publish = {provider:'generic',url:'https://desktop-gateway.example.invalid/maimi/updates/windows/',channel:'stable',useMultipleRangeRequest:false};
const configText = fs.readFileSync(path.join(packed,'resources/app-update.yml'),'utf8');
const updateUrl = /^url:\s*([^\s#]+)\s*$/m.exec(configText)?.[1];
if (updateUrl !== publish.url || new URL(publish.url).hostname.endsWith('.invalid'))
  throw Error('Configure the site and stage an application with its matching update origin');
delete process.env.CSC_LINK;
delete process.env.WIN_CSC_LINK;
process.env.CSC_IDENTITY_AUTO_DISCOVERY='false';
const result = await build({prepackaged:packed,projectDir,targets:Platform.WINDOWS.createTarget(['nsis'],Arch.x64),
  publish:'never',
  config:{appId:'com.megmeet.maimi',productName:'Maimi',electronVersion:pins.electron,
    directories:{output:release},publish,
    artifactName:'Maimi-Setup-${version}-${arch}.${ext}',compression:'normal',
    npmRebuild:false, win:{signAndEditExecutable:false,icon:path.join(packed,'resources','tray.ico')},
    nsis:{oneClick:false,allowToChangeInstallationDirectory:true,allowElevation:false,perMachine:false,deleteAppDataOnUninstall:false,shortcutName:'麦米办公助手',
      ...wizard,installerLanguages:['zh_CN'],
      installerIcon:path.join(packed,'resources','tray.ico'),createDesktopShortcut:'always',differentialPackage:true}}});
console.log(JSON.stringify({status:'BUILT',version:meta.version,result}));
