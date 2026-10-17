'use strict';
// Prepare the existing release tooling's expected local layout without signing
// or publishing. Large inputs remain in the task-owned C: build area.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),crypto=require('node:crypto');
const root=__dirname,repo=path.resolve(root,'../../..'),large='C:/Users/LI YUHAO/.codex/tmp/dsh020-r41-desktop',area=path.join(large,'handoff');
const validation=JSON.parse(fs.readFileSync(path.join(root,'validation.json')));assert.equal(validation.status,'PASS');
const build=JSON.parse(fs.readFileSync(path.join(root,'evidence/installer-build.json')));assert.equal(build.version,'0.13.0');assert.equal(build.dsh,'0.2.0-rc.2');
assert(!fs.existsSync(area),'Fresh immutable handoff required');
const desktop=path.join(area,'desktop-local'),ops=path.join(area,'ops/windows-installer');fs.mkdirSync(desktop,{recursive:true});fs.mkdirSync(ops,{recursive:true});
for(const name of ['package.json','updater.cjs','update-policy.json'])fs.copyFileSync(path.join(root,'app',name),path.join(desktop,name));
fs.mkdirSync(path.join(desktop,'evidence'));fs.mkdirSync(path.join(desktop,'dist'));fs.mkdirSync(path.join(area,'releases'));
for(const name of ['release-signing.cjs','verify-installer.cjs'])fs.copyFileSync(path.join(repo,'output/remembered-login-r40-20260930/desktop/package-build-final/ops/windows-installer',name),path.join(ops,name));
function junction(link,target){const real=fs.realpathSync(target);assert(!fs.existsSync(link));fs.symlinkSync(real,link,'junction');assert.equal(fs.realpathSync(link),real);}
junction(path.join(desktop,'dist/0.13.0-company'),path.join(large,'packages/0.13.0-company'));
junction(path.join(area,'releases/installer-0.13.0'),path.dirname(build.installer));
// This is build-only dependency resolution. No installer or server archive
// contains this junction or the pre-existing development dependency tree.
junction(path.join(desktop,'node_modules'),path.join(repo,'new-system/desktop-local/node_modules'));
const hash=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');assert.equal(hash(build.installer),build.sha256);
const result={status:'PASS',desktop_version:'0.13.0',runtime_version:'0.2.0-rc.2',installer:build.installer,installer_sha256:build.sha256,installer_bytes:build.bytes,ops_dir:ops,desktop_dir:desktop,build_area:area,company_requests:0,update_feed_created:false,authenticode:'NOT_SIGNED',employee_actual_upgrade:'PENDING'};
fs.writeFileSync(path.join(root,'build-result.json'),JSON.stringify(result,null,2)+'\n',{flag:'wx'});console.log(JSON.stringify(result));
