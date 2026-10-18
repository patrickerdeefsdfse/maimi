import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {verify} from './verify-runtime.mjs';

const digest=s=>createHash('sha256').update(s).digest('hex');
function fixture(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'r65-verify-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const manifest={schema:'maimi-native021-runtime-candidate-v1',runtime:'0.2.1-rc.2',official_packages_modified:0,modules_mode:'COPIED_COMPLETE_TREE',packages:{},module_files:{},company_source_targets:{}};
 function file(name,body){const p=path.join(root,name);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,body);return p;}
 for(const name of ['dsh','dsh-api-gateway','dsh-host-webserver','dsh-session','dsh-api-workspace-controller','dsh-client-ui-sidebar','dsh-client-ui-chat','libreoffice-kit','libreoffice-kit-wasm']){
  const full='@deepseek-ai/'+name,body=JSON.stringify({name:full,version:'0.2.1-rc.2'}),rel=full+'/package.json';
  manifest.packages[full]='0.2.1-rc.2';manifest.module_files[rel]={kind:'file',sha256:digest(body)};file('opt/dsh/node_modules/'+rel,body);
 }
 for(const name of ['/opt/dsh/package.json','/opt/dsh/package-lock.json','/opt/dsh/plugins/company-ui/index.mjs','/opt/dsh/plugins/company-ui/client.js','/opt/dsh/plugins/company-ui/package.json','/opt/personal-cloud/worker.mjs','/opt/personal-cloud/task-authority.mjs','/opt/personal-cloud/enterprise-profile.mjs','/opt/personal-cloud/community-profile.mjs','/opt/personal-cloud/company-workspace-controller.mjs']){
  file(name,'fixture');manifest.company_source_targets[name]=digest('fixture');
 }
 manifest.source_package_lock_sha256=digest('fixture');
 const save=()=>file('candidate-manifest.json',JSON.stringify(manifest));save();
 return {root,manifest,save,file};
}
test('complete inventory verifies and manifest pin is checked',t=>{
 const f=fixture(t);assert.equal(verify(f.root).status,'VERIFIED');assert.throws(()=>verify(f.root,'0'.repeat(64)),/MANIFEST_DIGEST/);
});
test('empty and missing required inventories rejected',t=>{
 const f=fixture(t);f.manifest.packages={};f.save();assert.throws(()=>verify(f.root),/EMPTY_REQUIRED/);
});
test('same-version modified package source rejected',t=>{
 const f=fixture(t);f.file('opt/dsh/node_modules/@deepseek-ai/dsh/package.json',JSON.stringify({name:'@deepseek-ai/dsh',version:'0.2.1-rc.2',extra:true}));assert.throws(()=>verify(f.root),/MODULE_CONTENT/);
});
test('extra file and removed file rejected',t=>{
 const f=fixture(t);const extra=f.file('opt/dsh/node_modules/injected.js','x');assert.throws(()=>verify(f.root),/UNEXPECTED_MODULE/);fs.unlinkSync(extra);fs.unlinkSync(path.join(f.root,'opt/dsh/node_modules/@deepseek-ai/dsh/package.json'));assert.throws(()=>verify(f.root),/MISSING_MODULE/);
});
test('missing company worker rejected',t=>{
 const f=fixture(t);delete f.manifest.company_source_targets['/opt/personal-cloud/worker.mjs'];f.save();assert.throws(()=>verify(f.root),/MISSING_REQUIRED_COMPANY/);
});
test('module symlink cannot escape runtime',t=>{
 const f=fixture(t);const rel='outside';fs.symlinkSync('/etc/hosts',path.join(f.root,'opt/dsh/node_modules',rel));f.manifest.module_files[rel]={kind:'symlink',target:'/etc/hosts'};f.save();assert.throws(()=>verify(f.root),/MODULE_LINK/);
});
