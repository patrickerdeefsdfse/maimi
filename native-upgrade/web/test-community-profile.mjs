import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm,symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {prepareCommunityProfile} from './community-profile.mjs';
const runtime=process.env.MAIMI_TEST_RUNTIME;
if(!runtime)throw Error('MAIMI_TEST_RUNTIME_REQUIRED');
const require=createRequire(pathToFileURL(path.join(runtime,'package.json')));
const yaml=require('yaml');
async function fixture(t,text){
  const home=await mkdtemp(path.join(tmpdir(),'maimi-native-profile-'));
  t.after(()=>rm(home,{recursive:true,force:true}));
  const dir=path.join(home,'profiles/web');await mkdir(dir,{recursive:true});
  const file=path.join(dir,'cordis.patch.yml');if(text!==undefined)await writeFile(file,text);
  return {home,file};
}
test('retire only legacy adapters, preserve unrelated native settings and byte backup',async t=>{
  const source='- id: agent-default-model\n  config: {provider: company, model: example}\n- insert:\n  - id: dshp-token-meter\n    name: old-token-adapter\n  - id: employee-extension\n    name: example\n- id: dshp-web-style\n  disabled: true\n';
  const {home,file}=await fixture(t,source);
  const result=await prepareCommunityProfile({home,yaml});
  assert.equal(result.retiredRows,2);
  const rows=yaml.parse(await readFile(file,'utf8'));
  assert.equal(rows[0].config.model,'example');assert.equal(rows[1].insert[0].id,'employee-extension');
  assert.equal(await readFile(file+'.before-megmeet-native021','utf8'),source);
  assert.deepEqual(await prepareCommunityProfile({home,yaml}),{changed:false});
});
test('untouched profiles remain byte identical',async t=>{
  const source='# user preference\n- id: tools\n  config: {mode: ptc}\n';
  const {home,file}=await fixture(t,source);
  assert.deepEqual(await prepareCommunityProfile({home,yaml}),{changed:false});
  assert.equal(await readFile(file,'utf8'),source);
});
test('malformed or duplicate YAML never replaces existing profile',async t=>{
  const source='- id: broken\n  id: duplicate\n';const {home,file}=await fixture(t,source);
  await assert.rejects(prepareCommunityProfile({home,yaml}),/COMMUNITY_PROFILE_INVALID/);
  assert.equal(await readFile(file,'utf8'),source);
});
test('symbolic link cannot redirect profile migration',async t=>{
  const {home,file}=await fixture(t);const outside=path.join(home,'outside.yml');
  await writeFile(outside,'[]\n');await symlink(outside,file);
  await assert.rejects(prepareCommunityProfile({home,yaml}),/COMMUNITY_PROFILE_INVALID/);
});
