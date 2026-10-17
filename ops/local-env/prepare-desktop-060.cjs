'use strict';
// Explicit source-to-candidate build, never modifies an installed employee app.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),cp=require('node:child_process');
const root=path.resolve(__dirname,'../..'),desktop=path.join(root,'desktop-local'),runtime=path.join(desktop,'runtime');
const version='0.6.0';
for(const file of ['package.json','package-lock.json']){
 const target=path.join(desktop,file),data=JSON.parse(fs.readFileSync(target));
 if(!['0.5.0',version].includes(data.version))throw Error('Unexpected candidate version');
 data.version=version;if(data.packages?.[''])data.packages[''].version=version;
 fs.writeFileSync(target,JSON.stringify(data,null,2)+'\n');
}
for(const name of ['brand-overlay-r2.mjs','enterprise-native.mjs','workspace-pinned-overlay.mjs','prepare-runtime.mjs','community-plugins.mjs','community-plugins-guard.mjs'])
 fs.copyFileSync(path.join(root,'dev-local/dsh',name),path.join(runtime,name));
function copyTree(source,target){
 fs.mkdirSync(target,{recursive:true});
 for(const entry of fs.readdirSync(source,{withFileTypes:true})){
  if(entry.isSymbolicLink())throw Error('Source link refused');
  if(entry.isDirectory())copyTree(path.join(source,entry.name),path.join(target,entry.name));
  else fs.copyFileSync(path.join(source,entry.name),path.join(target,entry.name));
 }
}
copyTree(path.join(root,'dev-local/dsh/vendor-community'),path.join(runtime,'vendor-community'));
const modules=path.join(runtime,'node_modules/@deepseek-ai');
for(const name of ['dsh-client-ui-brand-official','dsh-client-ui-conversation','dsh-client-ui-settings-models','dsh-client-ui-workspace'])
 fs.copyFileSync(path.join(runtime,'pristine',name+'.js'),path.join(modules,name,'lib/client.js'));
for(const [file,args] of [['brand-overlay-r2.mjs',[modules,path.join(runtime,'brand-adaptation.json')]],['prepare-runtime.mjs',[modules,runtime]]])
 cp.execFileSync(process.execPath,[path.join(runtime,file),...args],{stdio:'inherit'});
const file=path.join(runtime,'runtime-manifest.json'),manifest=JSON.parse(fs.readFileSync(file));
for(const [name,key] of [['release-manifest.json','releaseSha256'],['brand-adaptation.json','adaptationSha256']])
 manifest[key]=crypto.createHash('sha256').update(fs.readFileSync(path.join(runtime,name))).digest('hex');
fs.writeFileSync(file,JSON.stringify(manifest,null,2)+'\n');
cp.execFileSync(process.execPath,[path.join(desktop,'scripts/verify-dsh-candidate.cjs'),'--seal-local'],{stdio:'inherit'});
