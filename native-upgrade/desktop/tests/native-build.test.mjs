import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { inspectSource, makeBuildPlan, stageSource, regularFile, sha256, TARGET } from '../src/native-build.mjs';
import { migrationStatus, REQUIREMENTS } from '../src/activation-gate.mjs';

function fixture(t) {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'maimi-native-build-'));
  t.after(() => fs.rmSync(temporary, {recursive:true,force:true}));
  const source = path.join(temporary, 'source'); fs.mkdirSync(source);
  const entries = {
    'package.json':JSON.stringify({version:TARGET.version,packageManager:'pnpm@11.28.5',scripts:{'build:artifacts':'tsx scripts/build.ts --scope artifacts'}}),
    'apps/desktop/package.json':JSON.stringify({name:'@deepseek-ai/dsh-desktop',version:TARGET.version,scripts:{'package:win:x64:unsigned':'tsx scripts/package-target.ts win-x64 --unsigned'}}),
    'apps/desktop-host/package.json':JSON.stringify({version:TARGET.version}), 'apps/cli/package.json':JSON.stringify({version:TARGET.version}),
    'pnpm-lock.yaml':'lockfileVersion: 9.0\n', 'pnpm-workspace.yaml':'packages: []\n',
    'apps/desktop/src/main.ts':'export {}\n','apps/desktop/src/host-process.ts':'export {}\n',
    'apps/desktop-host/src/index.ts':'export {}\n','apps/desktop/README.md':'Synthetic fixture\n',
  };
  const files=Object.entries(entries).map(([relative,data]) => {
    const f=path.join(source,...relative.split('/'));fs.mkdirSync(path.dirname(f),{recursive:true});fs.writeFileSync(f,data);
    return {path:relative,bytes:Buffer.byteLength(data),sha256:sha256(data),mode:0o644};
  });
  const pins={schema:'maimi-dsh-native-desktop-source-lock-v1',version:TARGET.version,commit:TARGET.commit,source_archive_sha256:TARGET.sourceArchiveSha256,files};
  return {temporary,source,pins};
}
test('native exact version and source pins validate',t=>{const f=fixture(t);assert.equal(inspectSource(f.source,f.pins).status,'VERIFIED');});
test('source edits fail before build',t=>{const f=fixture(t);fs.appendFileSync(path.join(f.source,'apps/desktop/src/main.ts'),'BAD');assert.throws(()=>inspectSource(f.source,f.pins),/HASH_MISMATCH/);});
test('mixed runtime and desktop versions rejected',t=>{const f=fixture(t);fs.writeFileSync(path.join(f.source,'apps/cli/package.json'),'{"version":"0.2.0-rc.2"}');assert.throws(()=>inspectSource(f.source,f.pins),/VERSION_SET/);});
test('unreviewed mutable latest tags do not satisfy lock',t=>{const f=fixture(t);f.pins.version='latest';assert.throws(()=>inspectSource(f.source,f.pins),/LOCK_INVALID/);});
test('staging creates a new verified source candidate and preserves original',t=>{const f=fixture(t);const dest=path.join(f.temporary,'candidate');assert.equal(stageSource(f.source,dest,f.pins).status,'STAGED');assert.equal(inspectSource(f.source,f.pins).status,'VERIFIED');assert.throws(()=>stageSource(f.source,dest,f.pins),/ALREADY_EXISTS/);});
test('staging cannot enter source tree',t=>{const f=fixture(t);assert.throws(()=>stageSource(f.source,path.join(f.source,'candidate'),f.pins),/INSIDE_SOURCE/);});
test('traversal path and duplicate member rejected',t=>{const f=fixture(t);assert.throws(()=>regularFile(f.source,'../other'),/UNSAFE_SOURCE_PATH/);f.pins.files.push(f.pins.files[0]);assert.throws(()=>inspectSource(f.source,f.pins),/LOCK_INVALID/);});
test('build plan uses direct argv and pinned frozen install without publication',t=>{const f=fixture(t);const plan=makeBuildPlan({source:f.source,stage:path.join(f.temporary,'stage with spaces'),state:path.join(f.temporary,'state'),pnpmEntry:path.join(f.temporary,'pnpm.cjs')});assert.equal(plan.launch_allowed,false);assert.equal(plan.company_publish_allowed,false);assert.equal(plan.source_overlay_allowlist.length,0);assert(plan.commands.every(x=>x.shell===false));assert(plan.commands[0].args.includes('--frozen-lockfile'));assert(!plan.commands.some(x=>x.args.some(a=>a.startsWith('upload'))));});
test('state and source cannot overlap',t=>{const f=fixture(t);assert.throws(()=>makeBuildPlan({source:f.source,stage:path.join(f.temporary,'stage'),state:path.join(f.source,'home'),pnpmEntry:path.join(f.temporary,'pnpm.cjs')}),/OVERLAP/);});
test('synthetic local skills do not remove the cloud entry',()=>{const evidence=Object.fromEntries(REQUIREMENTS.map(x=>[x,'PASS_SYNTHETIC']));assert.equal(migrationStatus(evidence).keepCloudEntry,true);assert.equal(migrationStatus(evidence).publishCompanyInstaller,false);});
test('unknown evidence statuses cannot accidentally qualify a release',()=>assert.throws(()=>migrationStatus({nativeRuntime:true}),/INVALID_MIGRATION_EVIDENCE/));
test('all company evidence is necessary for publication',()=>{const evidence=Object.fromEntries(REQUIREMENTS.map(x=>[x,'PASS_COMPANY']));assert.equal(migrationStatus(evidence).publishCompanyInstaller,true);delete evidence.iamAccountIsolation;assert.equal(migrationStatus(evidence).publishCompanyInstaller,false);});
