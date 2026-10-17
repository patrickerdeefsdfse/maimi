'use strict';
// Local unsigned build only. No company publication, release feed or signing.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=__dirname,app=path.join(root,'app'),repo=path.resolve(root,'../../..');
const buildRequire=require('node:module').createRequire(path.join(repo,'new-system/ops/windows-installer/package.json'));
const {build,Platform,Arch}=buildRequire('electron-builder');
const {writeInstallerInclude}=require('./installer/path-policy.cjs');
(async()=>{
const version=JSON.parse(fs.readFileSync(path.join(app,'package.json'))).version;
const prepackaged=path.join("C:\\Users\\LI YUHAO\\.codex\\tmp\\dsh020-r41-desktop\\packages",version+'-company','MEGMEET AI-win32-x64');
const evidence=JSON.parse(fs.readFileSync(path.join(app,'artifact-company.json')));
if(evidence.version!==version||evidence.packagedRuntimeVerified!==true||evidence.runtime.dsh!=='0.2.0-rc.2')throw Error('Packaged candidate/runtime differs');
const output=path.join("C:\\Users\\LI YUHAO\\.codex\\tmp\\dsh020-r41-desktop\\build-only",'installer-'+version);if(fs.existsSync(output))throw Error('Immutable build output already exists');fs.mkdirSync(output,{recursive:true});
const policy=writeInstallerInclude(prepackaged,path.join(output,'payload-path-policy.nsh'));
const files=await build({projectDir:app,prepackaged,targets:Platform.WINDOWS.createTarget('nsis',Arch.x64),publish:'never',config:{appId:'com.megmeet.ai',productName:'麦米办公助手',executableName:'MEGMEET-AI',electronVersion:'44.4.1',directories:{output,buildResources:path.join(app,'assets')},artifactName:'MEGMEET-AI-Setup-${version}-${arch}.${ext}',win:{icon:path.join(app,'assets/app-icon.ico'),signAndEditExecutable:false,target:[{target:'nsis',arch:['x64']}]},nsis:{oneClick:false,perMachine:false,allowElevation:false,allowToChangeInstallationDirectory:true,packElevateHelper:false,runAfterFinish:true,differentialPackage:false,useZip:true,deleteAppDataOnUninstall:false,createDesktopShortcut:'always',createStartMenuShortcut:true,shortcutName:'麦米办公助手',uninstallDisplayName:'麦米办公助手',include:policy.include,installerLanguages:['zh_CN','en_US'],language:'2052'},compression:'normal',npmRebuild:false,publish:null}});
const installer=files.find(f=>f.endsWith('.exe'));const result={status:'BUILT_LOCAL_NOT_PUBLISHED',version,dsh:evidence.runtime.dsh,installer,bytes:fs.statSync(installer).size,sha256:crypto.createHash('sha256').update(fs.readFileSync(installer)).digest('hex'),authenticode:'NOT_SIGNED',releaseFeedCreated:false,companyRequests:0,policy};fs.writeFileSync(path.join(root,'evidence/installer-build.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
})().catch(e=>{console.error(e);process.exitCode=1;});
