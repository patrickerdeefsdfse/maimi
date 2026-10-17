'use strict';
// The runtime and executable are unchanged; rebuild the exact application ASAR
// after the final managed model capability-query compatibility correction.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=__dirname,repo=path.resolve(root,'../../..'),large='C:/Users/LI YUHAO/.codex/tmp/dsh020-r41-desktop';
const buildRequire=require('node:module').createRequire(path.join(repo,'new-system/desktop-local/package.json'));
const asar=buildRequire('@electron/asar'),hash=f=>crypto.createHash('sha256').update(fs.readFileSync(f)).digest('hex');
(async()=>{
 const packaged=path.join(large,'packages/0.13.0-company/MEGMEET AI-win32-x64'),archive=path.join(packaged,'resources/app.asar');
 const stage=fs.mkdtempSync(path.join(large,'app-reseal-'));
 asar.extractAll(archive,stage);
 for(const name of ['enterprise-model-policy.mjs','dsh-adapter.cjs'])fs.copyFileSync(path.join(root,'app',name),path.join(stage,name));
 const next=path.join(large,'app-reseal-final.asar');assert(!fs.existsSync(next));await asar.createPackage(stage,next);
 const old=path.join(large,'app-before-final-capability-policy.asar');assert(!fs.existsSync(old));fs.copyFileSync(archive,old);fs.copyFileSync(next,archive);
 const runtime=path.join(large,'runtime-win32-x64'),target=path.join(packaged,'resources/runtime');
 for(const name of ['runtime-manifest.json','compatibility-manifest.json'])fs.copyFileSync(path.join(runtime,name),path.join(target,name));
 require('./app/dsh-adapter.cjs').validateRuntime(target,{full:true});
 const artifact=JSON.parse(fs.readFileSync(path.join(root,'app/artifact-company.json')));
 artifact.runtime=JSON.parse(fs.readFileSync(path.join(runtime,'runtime-manifest.json')));
 for(const row of artifact.binaries){row.bytes=fs.statSync(row.path).size;row.sha256=hash(row.path);}
 fs.writeFileSync(path.join(root,'app/artifact-company.json'),JSON.stringify(artifact,null,2)+'\n');
 const previous=path.join(large,'build-only/installer-0.13.0'),saved=path.join(large,'build-only/superseded-pre-image-policy');
 assert(path.resolve(previous).startsWith(path.resolve(large)+path.sep));assert(!fs.existsSync(saved));fs.renameSync(previous,saved);
 console.log('FINAL_MODEL_POLICY_PAYLOAD_RESEALED_REBUILD_REQUIRED');
})().catch(e=>{console.error(e);process.exitCode=1;});
