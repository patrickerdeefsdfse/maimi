import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {createCompanyDeviceLogin} from '../src/company-device-login.mjs';

export function syntheticSafeStorage(){
 const key=crypto.randomBytes(32);
 return {isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'gnome_libsecret',
 encryptString(value){const nonce=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,nonce);return Buffer.concat([nonce,cipher.update(value,'utf8'),cipher.final(),cipher.getAuthTag()]);},
 decryptString(value){const decipher=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));decipher.setAuthTag(value.subarray(-16));return Buffer.concat([decipher.update(value.subarray(12,-16)),decipher.final()]).toString('utf8');}};
}
function fixture(t){
 const directory=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-device-r65-test-'));
 t.after(()=>fs.rmSync(directory,{recursive:true,force:true}));
 const safeStorage=syntheticSafeStorage(),calls={signIn:0,resume:[],revoke:0};
 const auth=device=>({token:'synthetic-access-parent-only',expiresAt:Date.now()+120000,user:{user_no:'M00000001',account_no:'synthetic'},...(device?{remembered:{deviceId:device.deviceId,expiresAt:0}}:{})});
 const api={capabilities:async()=>true,signIn:async(_browser,{device}={})=>{calls.signIn++;return auth(device);},resumeDevice:async device=>{calls.resume.push(device);return auth(device);},revokeDevice:async()=>{calls.revoke++;},signOut:async()=>{}};
 const options={directory,safeStorage,api};
 const controller=()=>createCompanyDeviceLogin(options);
 return {directory,safeStorage,calls,api,auth,options,controller};
}
const input={openBrowser:async()=>{throw Error('SYNTHETIC_API_MUST_NOT_OPEN_BROWSER');}};
test('fresh login stores encrypted device proof, normal close retains it and restart resumes',async t=>{
 const f=fixture(t),first=f.controller();const signed=await first.authenticate(input);assert.equal(f.calls.signIn,1);assert.equal(first.status().remembered,true);
 const ciphertext=fs.readFileSync(path.join(f.directory,'remembered-device-v1.bin'));assert.ok(!ciphertext.includes(Buffer.from('synthetic-access')));assert.ok(!ciphertext.includes(Buffer.from('M00000001')));
 const plain=f.safeStorage.decryptString(ciphertext);assert.ok(!plain.includes(signed.token));assert.ok(!plain.includes('expires_in'));
 await first.stop();assert.equal(f.calls.revoke,0);
 const second=f.controller();assert.equal((await second.authenticate(input)).user.user_no,'M00000001');assert.equal(f.calls.signIn,1);assert.equal(f.calls.resume.length,1);await second.stop();
});
test('ambiguous resume leaves the same journal for next launch without fresh sign-in',async t=>{
 const f=fixture(t);await f.controller().authenticate(input);const requests=[];
 f.api.resumeDevice=async record=>{requests.push(record);throw Object.assign(Error('synthetic unavailable'),{status:503});};
 for(let i=0;i<2;i++){const c=f.controller();await assert.rejects(c.authenticate(input));await c.stop();}
 assert.equal(f.calls.signIn,1);assert.equal(requests[0].pending.requestId,requests[1].pending.requestId);assert.equal(requests[0].pending.nextToken,requests[1].pending.nextToken);
});
test('revoked device falls back to a distinct browser enrollment',async t=>{
 const f=fixture(t);await f.controller().authenticate(input);const old=JSON.parse(f.safeStorage.decryptString(fs.readFileSync(path.join(f.directory,'remembered-device-v1.bin')))).active.deviceId;
 f.api.resumeDevice=async()=>{throw Object.assign(Error('denied'),{status:401});};
 const c=f.controller();await c.authenticate(input);assert.equal(f.calls.signIn,2);const current=JSON.parse(f.safeStorage.decryptString(fs.readFileSync(path.join(f.directory,'remembered-device-v1.bin')))).active.deviceId;assert.notEqual(old,current);await c.stop();
});
test('explicit offline sign-out blocks old proof and queues revocation',async t=>{
 const f=fixture(t),c=f.controller();const auth=await c.authenticate(input);f.api.revokeDevice=async()=>{throw Error('offline');};f.api.signOut=async()=>{throw Error('offline');};
 assert.equal(await c.signOut(auth),false);assert.equal(c.status().remembered,false);assert.equal(c.status().pendingRevocation,true);await c.stop();
 const next=f.controller();await next.authenticate(input);assert.equal(f.calls.resume.length,0);assert.equal(f.calls.signIn,2);await next.stop();
});
test('temporarily unavailable OS encryption preserves file and uses ephemeral login',async t=>{
 const f=fixture(t);await f.controller().authenticate(input);const file=path.join(f.directory,'remembered-device-v1.bin'),before=fs.readFileSync(file);f.safeStorage.isEncryptionAvailable=()=>false;
 const c=f.controller();const result=await c.authenticate(input);assert.equal(result.remembered,undefined);assert.equal(f.calls.resume.length,0);assert.deepEqual(fs.readFileSync(file),before);await c.stop();
});
test('Linux basic_text backend never stores a credential',async t=>{
 const f=fixture(t);f.safeStorage.getSelectedStorageBackend=()=> 'basic_text';const c=createCompanyDeviceLogin({...f.options,platform:'linux'});assert.equal((await c.authenticate(input)).remembered,undefined);assert.equal(fs.existsSync(path.join(f.directory,'remembered-device-v1.bin')),false);await c.stop();
});
test('corrupted encrypted proof is preserved and does not cause new enrollment',async t=>{
 const f=fixture(t),file=path.join(f.directory,'remembered-device-v1.bin');fs.writeFileSync(file,'synthetic-invalid');const c=f.controller();await assert.rejects(c.authenticate(input),/DEVICE_DECRYPTION_UNAVAILABLE/);assert.equal(f.calls.signIn,0);assert.equal(fs.readFileSync(file,'utf8'),'synthetic-invalid');await c.stop();
});
test('caller cancellation before authentication sends no request',async t=>{
 const f=fixture(t),c=f.controller(),abort=new AbortController();abort.abort();await assert.rejects(c.authenticate({...input,signal:abort.signal}));assert.equal(f.calls.signIn,0);assert.equal(f.calls.resume.length,0);await c.stop();
});
test('cancel after enrollment prevents credential activation and queues proof for revocation',async t=>{
 const f=fixture(t),c=f.controller(),abort=new AbortController();f.api.signIn=async(_browser,{device})=>{abort.abort();return f.auth(device);};await assert.rejects(c.authenticate({...input,signal:abort.signal}));assert.equal(c.status().remembered,false);await c.stop();
});
test('unsupported server capability uses ephemeral IAM without device write',async t=>{
 const f=fixture(t);f.api.capabilities=async()=>false;const c=f.controller();assert.equal((await c.authenticate(input)).remembered,undefined);assert.equal(fs.existsSync(path.join(f.directory,'remembered-device-v1.bin')),false);await c.stop();
});
