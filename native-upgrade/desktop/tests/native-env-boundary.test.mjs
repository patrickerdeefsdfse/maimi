import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';

const runtimeRoot=process.env.DSH_R65_RUNTIME??path.resolve(import.meta.dirname,'../../upstream',process.platform==='win32'?'runtime-win':'runtime-linux');
const runtime=path.join(runtimeRoot,'node_modules/@deepseek-ai');
const {scrubbedParentEnv}=await import(pathToFileURL(path.join(runtime,'dsh-subprocess/lib/index.js')).href);
// Resolve the actual childEnv export used by this platform's pinned package.
const localIndex=fs.readFileSync(path.join(runtime,'dsh-subprocess-local/lib/index.js'),'utf8');
const binding=[...localIndex.matchAll(/import \{([^}]+)\} from ["'](\.\/runner-launch-[A-Za-z0-9_-]+\.js)["']/g)]
 .map(match=>({file:match[2],alias:match[1].match(/(?:^|,)\s*([A-Za-z_$][\w$]*)\s+as\s+childEnv\s*(?:,|$)/)?.[1]})).find(value=>value.alias);
assert.ok(binding,'pinned native local module must import childEnv from its runtime chunk');
const childEnv=(await import(pathToFileURL(path.join(runtime,'dsh-subprocess-local/lib',binding.file)).href))[binding.alias];
assert.equal(typeof childEnv,'function');

test('local model token follows actual pinned native scrub and does not reach real child process',()=>{
 for(const name of ['dsh-subprocess','dsh-subprocess-local'])assert.equal(JSON.parse(fs.readFileSync(path.join(runtime,name,'package.json'),'utf8')).version,'0.2.1-rc.2');
 const key='MAIMI_LOCAL_MODEL_TOKEN',previous=process.env[key];
 process.env[key]='synthetic-boundary-test-no-business-credential';
 try {
  const scrubbed=scrubbedParentEnv();assert.equal(scrubbed[key],undefined);
  const child=childEnv({MAIMI_SYNTHETIC_PUBLIC_MARKER:'visible'});assert.equal(child[key],undefined);
  const answer=JSON.parse(execFileSync(process.execPath,['-e',"process.stdout.write(JSON.stringify({credentialPresent:Object.prototype.hasOwnProperty.call(process.env,'MAIMI_LOCAL_MODEL_TOKEN'),publicMarker:process.env.MAIMI_SYNTHETIC_PUBLIC_MARKER==='visible'}))"],{env:child,encoding:'utf8',timeout:10000}));
  assert.deepEqual(answer,{credentialPresent:false,publicMarker:true});
 } finally {if(previous===undefined)delete process.env[key];else process.env[key]=previous;}
});

test('both IPC initialization and model provider select scrubbed TOKEN name',()=>{
 const entry=fs.readFileSync(new URL('../src/company-host-child.mjs',import.meta.url),'utf8');
 const host=fs.readFileSync(new URL('../src/native-company-host.mjs',import.meta.url),'utf8');
 assert.ok(entry.includes('process.env.MAIMI_LOCAL_MODEL_TOKEN=message.capability'));
 assert.ok(host.includes("apiKeyEnv:'MAIMI_LOCAL_MODEL_TOKEN'"));
 assert.ok(!entry.includes('MAIMI_LOCAL_MODEL_CAPABILITY'));assert.ok(!host.includes('MAIMI_LOCAL_MODEL_CAPABILITY'));
});
