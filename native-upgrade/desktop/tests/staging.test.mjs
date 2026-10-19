import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';

test('explicit staging root receives exact native main and all company auth files without rewriting frozen receipts',()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-desktop-overlay-'));
 try {
  const stage=path.join(root,'stage'),receipts=path.join(root,'receipts');
  for(const dir of ['apps/desktop/src','apps/desktop/scripts','apps/desktop-host/src'])fs.mkdirSync(path.join(stage,dir),{recursive:true});
  fs.writeFileSync(path.join(stage,'.maimi-native-candidate.json'),'{}');
  const here=fileURLToPath(new URL('..',import.meta.url));
  const frozen=path.join(here,'patch-generation.json'),before=fs.readFileSync(frozen);
  execFileSync('python',[path.join(here,'scripts/prepare-native-shell.py'),'--stage-root',stage,'--receipt-directory',receipts],{encoding:'utf8'});
  const main=execFileSync('python',[path.join(here,'scripts/refresh-patch.py'),'--emit-main'],{encoding:'utf8',maxBuffer:2*1024*1024});
  assert.equal(fs.readFileSync(path.join(stage,'apps/desktop/src/main.ts'),'utf8'),main);
  for(const file of ['company-shell-host.ts','company-signout.ts'])assert.deepEqual(fs.readFileSync(path.join(stage,'apps/desktop/src',file)),fs.readFileSync(path.join(here,'src',file)));
  for(const file of ['company-device-login.mjs','company-desktop-session.mjs','company-auth/remembered-store.cjs','company-auth/device-session.cjs'])assert.deepEqual(fs.readFileSync(path.join(stage,'apps/desktop/maimi',file)),fs.readFileSync(path.join(here,'src',file)));
  assert.deepEqual(fs.readFileSync(frozen),before);
  const receipt=JSON.parse(fs.readFileSync(path.join(receipts,'shell-integration.json'),'utf8'));
  for(const file of receipt.changed_upstream_files)assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(stage,file.path))).digest('hex'),file.candidate_sha256);
  assert.equal(receipt.official_update_feed_enabled,false);
 } finally {fs.rmSync(root,{recursive:true,force:true});}
});
