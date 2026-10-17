'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {spawnSync}=require('node:child_process');
const root=path.resolve(__dirname,'../..'),desktop=path.join(root,'desktop-local');
const args=process.argv.slice(2);
if(args.length && (args.length!==2 || args[0]!=='--site'))throw Error('Usage: verify-installer.cjs [--site candidate-site-directory]');
const site=args.length?path.resolve(args[1]):path.join(root,'ops/desktop-distribution/site');
const version=JSON.parse(fs.readFileSync(path.join(desktop,'package.json'))).version;
const installer=path.join(root,'releases',`installer-${version}`,`MEGMEET-AI-Setup-${version}-x64.exe`);
const candidate=path.join(desktop,'dist',version+'-company','MEGMEET AI-win32-x64');
const stage=fs.mkdtempSync(path.join(os.tmpdir(),'megmeet-final-installer-audit-'));
const seven=path.join(process.env.ProgramFiles,'7-Zip','7z.exe');
const run=args=>{const r=spawnSync(seven,args,{windowsHide:true,encoding:'utf8',timeout:180000,maxBuffer:32*1024*1024});if(r.status!==0)throw Error('7-Zip verification failed: '+r.stderr);return r.stdout;};
const hash=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
run(['x',installer,'-o'+stage,'$PLUGINSDIR/app-64.7z','$PLUGINSDIR/app-64.zip','-y','-bso0','-bsp0']);
const archives=['app-64.7z','app-64.zip'].map(name=>path.join(stage,'$PLUGINSDIR',name)).filter(file=>fs.existsSync(file));
if(archives.length!==1)throw Error('Installer must contain exactly one supported x64 payload');
const archive=archives[0];
run(['t',archive,'-bso0','-bsp0']);
const payload=path.join(stage,'payload');
const remote=fs.existsSync(path.join(candidate,'resources/runtime/office-remote/office-remote-manifest.json'));
const files=['MEGMEET-AI.exe','resources/app.asar','resources/app-icon.ico',
 'resources/runtime/runtime-manifest.json','resources/runtime/release-manifest.json','resources/runtime/compatibility-manifest.json','resources/runtime/brand-adaptation.json',
 'resources/runtime/toolkit/toolkit-manifest.json','resources/runtime/toolkit/session-tools.mjs','resources/runtime/toolkit/adapter/index.mjs','resources/runtime/toolkit/project-bridge.mjs','resources/runtime/enterprise-native.mjs','resources/runtime/assets/megmeet-wordmark.svg','resources/runtime/node_modules/@deepseek-ai/dsh-client-ui-brand-official/lib/client.js','resources/runtime/node_modules/@deepseek-ai/dsh-client-ui-conversation/lib/client.js'];
if(remote){
 const manifest=JSON.parse(fs.readFileSync(path.join(candidate,'resources/runtime/office-remote/office-remote-manifest.json')));
 files.push('resources/runtime/office-remote/office-remote-manifest.json',...Object.keys(manifest.files).map(n=>'resources/runtime/'+n));
 const listing=run(['l',archive,'-slt']);
 if(/^Path = resources[\\/]runtime[\\/](?:office[\\/]|node_modules[\\/]@deepseek-ai[\\/]libreoffice-kit(?:-[^\\/]+)?[\\/])/m.test(listing))throw Error('Remote installer unexpectedly contains heavy office components');
}else files.push('resources/runtime/office/office-manifest.json','resources/runtime/office/engine.py',
 'resources/runtime/office/adapter/index.mjs','resources/runtime/office/adapter/adapter.mjs','resources/runtime/office/adapter/transport.mjs');
run(['x',archive,'-o'+payload,...files,'-y','-bso0','-bsp0']);
for(const file of files)if(hash(path.join(payload,file))!==hash(path.join(candidate,file)))throw Error('Installer differs from audited application: '+file);
const asar=require('node:module').createRequire(path.join(desktop,'package.json'))('@electron/asar');
const packagedPolicy=JSON.parse(asar.extractFile(path.join(payload,'resources/app.asar'),'update-policy.json'));
const feed=fs.readFileSync(path.join(site,'updates/company-win-x64.json'),'utf8');
const release=require('../../desktop-local/updater.cjs').verifyManifest(feed,packagedPolicy);
if(release.version!==version || release.artifact.sha256!==hash(installer) || release.artifact.bytes!==fs.statSync(installer).size)throw Error('Installed public key does not authenticate this published release');
const evidence={version,site,officeMode:remote?'remote':'local',heavyOfficeAbsent:remote?'PASS':'not applicable',scope:'final NSIS payload CRC and audited executable/ASAR/runtime manifests; no installation or company network',
 payloadArchive:path.basename(archive),wholePayloadCRC:'PASS',payloadSourceMatch:files,embeddedPublicKeyAuthenticatesRelease:'PASS',installerSHA256:hash(installer),bytes:fs.statSync(installer).size,
 WindowsAuthenticode:'NOT_SIGNED',companyDeployment:'NOT_PERFORMED'};
fs.writeFileSync(path.join(desktop,'evidence',`final-installer-${version}-audit.json`),JSON.stringify(evidence,null,2)+'\n');
console.log('FINAL_NSIS_PAYLOAD_AND_PINNED_FEED: PASS');
