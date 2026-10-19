import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {signInAndStartCompanyNativeSession} from '../src/company-desktop-session.mjs';
const require=createRequire(import.meta.url),api=require('../src/company-auth/auth.cjs');

test('real native Host starts twice with encrypted enrollment then remembered restore, without company calls',{timeout:210000},async t=>{
 const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-native-device-test-'));
 const original={...api},key=crypto.randomBytes(32),calls={signIn:0,resume:0,modelCatalog:0,signOut:0};let first,second;
 const safeStorage={isEncryptionAvailable:()=>true,getSelectedStorageBackend:()=> 'gnome_libsecret',encryptString(value){const nonce=crypto.randomBytes(12),cipher=crypto.createCipheriv('aes-256-gcm',key,nonce);return Buffer.concat([nonce,cipher.update(value),cipher.final(),cipher.getAuthTag()]);},decryptString(value){const decipher=crypto.createDecipheriv('aes-256-gcm',key,value.subarray(0,12));decipher.setAuthTag(value.subarray(-16));return Buffer.concat([decipher.update(value.subarray(12,-16)),decipher.final()]).toString('utf8');}};
 const auth=device=>({token:'synthetic-private-parent-access',expiresAt:Date.now()+120000,user:{user_no:'M00000001',account_no:'synthetic'},remembered:{deviceId:device.deviceId,expiresAt:0}});
 api.capabilities=async()=>true;
 api.signIn=async(_browser,{device})=>{calls.signIn++;return auth(device);};
 api.resumeDevice=async device=>{calls.resume++;return auth(device);};
 api.revokeDevice=async()=>{};api.signOut=async()=>{calls.signOut++;};
 api.jsonRequest=async endpoint=>{assert.equal(endpoint,'/v1/personal-model/models');calls.modelCatalog++;return {value:{data:[{id:'deepseek-v4.1-flash'}]}};};
 const runtimeRoot=process.env.DSH_R65_RUNTIME??path.resolve(import.meta.dirname,'../../upstream',process.platform==='win32'?'runtime-win':'runtime-linux');
 const options={runtimeRoot,stateRoot:path.join(tmp,'isolated'),safeStorage,signal:t.signal,openBrowser:async()=>{throw Error('UNEXPECTED_BROWSER');}};
 try{
  first=await signInAndStartCompanyNativeSession(options);t.diagnostic('First real native Host ready after synthetic enrollment');assert.equal(first.hasCompanySession(),true);assert.equal(first.deviceStatus().remembered,true);assert.equal(calls.signIn,1);assert.equal(calls.resume,0);
  const exchange=await fetch(first.url,{redirect:'manual'});assert.equal(exchange.status,303);assert.equal((await first.inspectQuit()).activeTasks,false);
  await first.stop();assert.equal(calls.signOut,0);
  second=await signInAndStartCompanyNativeSession(options);assert.equal(calls.signIn,1);assert.equal(calls.resume,1);assert.equal(second.home,first.home);assert.equal(second.hasCompanySession(),true);
  assert.equal(await second.signOut(),true);assert.equal(calls.signOut,1);assert.equal(second.hasCompanySession(),false);assert.equal(second.deviceStatus().remembered,false);await assert.rejects(fetch(second.url));
 }finally{await first?.stop();await second?.stop();Object.assign(api,original);fs.rmSync(tmp,{recursive:true,force:true,maxRetries:3});}
});
