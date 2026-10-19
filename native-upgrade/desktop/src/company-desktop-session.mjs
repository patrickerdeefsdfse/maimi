import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fork} from 'node:child_process';
import {createRequire} from 'node:module';
import {createCompanyModelBridge} from './company-model-bridge.mjs';
import {createLocalEnterpriseBridge} from './local-enterprise/bridge.mjs';
import {createCompanyDeviceLogin} from './company-device-login.mjs';
const require=createRequire(import.meta.url);
const companyAuth=require('./company-auth/auth.cjs');
const MARKER='maimi-native-desktop-isolated-candidate-state-v1';
const accountKey=owner=>crypto.createHash('sha256').update('iam:'+owner).digest('hex').slice(0,32);
function stateDirectory(root){
 if(!path.isAbsolute(root))throw Error('ABSOLUTE_ISOLATED_STATE_REQUIRED');
 if(fs.existsSync(root)){
  if(fs.lstatSync(root).isSymbolicLink()||fs.readFileSync(path.join(root,'.maimi-candidate-state'),'utf8')!==MARKER)throw Error('STATE_IS_NOT_ISOLATED_CANDIDATE');
 }else{fs.mkdirSync(root,{recursive:true});fs.writeFileSync(path.join(root,'.maimi-candidate-state'),MARKER,{flag:'wx'});}
}
function validModels(models){return Array.isArray(models)&&models.length>0&&models.every(x=>x&&/^[A-Za-z0-9._-]{1,100}$/.test(x.id));}
function ownedDirectory(root,...segments){
 let result=root;
 for(const segment of segments){result=path.join(result,segment);if(fs.existsSync(result)){const s=fs.lstatSync(result);if(s.isSymbolicLink()||!s.isDirectory())throw Error('LINKED_CANDIDATE_STATE');}else fs.mkdirSync(result);}
 return result;
}

/** Ready-message adapter for the official Electron HostProcess. Never returns a
 * company token, replaces a native UI bundle, or accesses an installed profile. */
export async function startCompanyNativeSession({auth,models,runtimeRoot,stateRoot,primaryRuntime,packageManager,gatewayOrigin='https://platform-ai.megmeet.com',testOnly=false,onStatus=()=>{},onFailure=()=>{},signal}){
 signal?.throwIfAborted();
 if(!auth||!/^M\d{8}$/.test(auth.user?.user_no)||typeof auth.token!=='string'||!auth.token||!(auth.expiresAt>Date.now())||!validModels(models))throw Error('VERIFIED_COMPANY_AUTH_REQUIRED');
 stateDirectory(stateRoot);
 const owner=auth.user.user_no;
 const home=ownedDirectory(stateRoot,'accounts',accountKey(owner));
 const profileRoot=path.join(ownedDirectory(home,'native-profiles'),crypto.randomUUID());
 let active=true,stopping,readyResolve,readyReject,reportedReady=false;
 let nextControl=1;const controls=new Map();
 const session={owner,generation:1,token:auth.token,expiresAt:auth.expiresAt,models};
 let bridge,enterprise,child;
 try{
  bridge=await createCompanyModelBridge({getAuthorization:()=>active?{...session,expiresAt:auth.expiresAt}:null,gatewayOrigin,testOnly});
  signal?.throwIfAborted();
  if(['M00003307','M00001069','M00096389'].includes(owner))enterprise=await createLocalEnterpriseBridge({getAuthorization:()=>active?{...session,expiresAt:auth.expiresAt}:null,gatewayOrigin,testOnly});
  signal?.throwIfAborted();
  const env={};for(const key of ['PATH','Path','SystemRoot','TEMP','TMP','APPDATA','LOCALAPPDATA','USERPROFILE'])if(process.env[key])env[key]=process.env[key];
  if(process.versions.electron)env.ELECTRON_RUN_AS_NODE='1';
  child=fork(new URL('./company-host-child.mjs',import.meta.url),[],{execArgv:['--expose-internals'],env,silent:true});
 }catch(error){
  // No child/stop lifecycle exists yet. Reclaim every opened listener if bridge
  // creation, early cancellation or synchronous fork setup fails.
  await Promise.allSettled([bridge?.close(),enterprise?.close()]);
  active=false;session.generation++;
  throw error;
 }
 // Native startup output is consumed without forwarding user paths or diagnostics.
 child.stdout.resume();child.stderr.resume();
 const ready=new Promise((resolve,reject)=>{readyResolve=resolve;readyReject=reject;});
 const startupTimeout=setTimeout(()=>readyReject(Error('NATIVE_HOST_START_TIMEOUT')),90000);startupTimeout.unref();
 let shutdownComplete=false;
 const stopped=new Promise(resolve=>child.once('exit',resolve));
 const stop=()=>stopping??=(async()=>{
  clearTimeout(startupTimeout);signal?.removeEventListener('abort',aborted);
  for(const request of controls.values()){clearTimeout(request.timer);request.reject(Error('NATIVE_HOST_STOPPING'));}controls.clear();
  // Close admission and cancel outstanding enterprise work while the existing
  // IAM authorization can still retire its server-side connection grants.
  await Promise.allSettled([bridge.close(),enterprise?.close()]);
  active=false;session.generation++;
  if(child.connected)child.send({type:'shutdown'});
  await Promise.race([stopped,new Promise(resolve=>{const timer=setTimeout(resolve,10000);timer.unref();})]);
  if(child.exitCode===null&&!child.killed)child.kill();
  await Promise.race([stopped,new Promise((_,reject)=>{const timer=setTimeout(()=>reject(Error('NATIVE_HOST_STOP_TIMEOUT')),5000);timer.unref();})]);
  onStatus('stopped');
 })();
 const aborted=()=>{readyReject(Error('DESKTOP_START_CANCELLED'));void stop();};
 signal?.addEventListener('abort',aborted,{once:true});
 child.on('error',()=>readyReject(Error('NATIVE_HOST_START_FAILED')));
 child.on('exit',()=>{clearTimeout(startupTimeout);if(!shutdownComplete){readyReject(Error('NATIVE_HOST_EXITED'));if(reportedReady&&!stopping)onFailure(Error('NATIVE_HOST_EXITED'));}void stop();});
 child.on('message',message=>{
  if(message?.type==='bootstrap-ready')child.send({type:'maimi-native-start',home,profileRoot,runtimeRoot,primaryRuntime,packageManager,modelBase:bridge.origin+'/v1',modelId:models[0].id,capability:bridge.capability,...enterprise?{enterprise:{endpoint:enterprise.origin,nativeCapability:enterprise.nativeCapability}}:{}});
  if(message?.type==='fatal')readyReject(Error('NATIVE_HOST_START_FAILED'));
  if(message?.type==='shutdown-complete')shutdownComplete=true;
  if(Number.isSafeInteger(message?.requestId)&&controls.has(message.requestId)){
   const request=controls.get(message.requestId);controls.delete(message.requestId);clearTimeout(request.timer);
   if(message.type!==request.type||message.error)request.reject(Error('NATIVE_TASK_STATE_UNAVAILABLE'));else request.resolve(message);
  }
  if(message?.type==='ready'){
   try{const url=new URL(message.url);if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||!url.port||url.username||url.password)throw Error();clearTimeout(startupTimeout);reportedReady=true;readyResolve({url:message.url,injections:message.injections});}
   catch{readyReject(Error('NATIVE_READY_URL_INVALID'));}
  }
 });
 const control=(type,action)=>new Promise((resolve,reject)=>{
  if(!active||!child.connected||stopping){reject(Error('NATIVE_HOST_NOT_ACTIVE'));return;}
  const requestId=nextControl++;
  const timer=setTimeout(()=>{controls.delete(requestId);reject(Error('NATIVE_CONTROL_TIMEOUT'));},2000);timer.unref();
  controls.set(requestId,{resolve,reject,timer,type});child.send({type,requestId,...action===undefined?{}:{action}});
 });
 try{signal?.throwIfAborted();const page=await ready;if(auth.expiresAt<=Date.now())throw Error('COMPANY_SESSION_EXPIRED_DURING_START');onStatus('ready');return {...page,home,stop,hasCompanySession:()=>active&&auth.expiresAt>Date.now(),inspectQuit:async()=>{const v=await control('quit-inspection');if(typeof v.activeTasks!=='boolean'||typeof v.scheduledTasks!=='boolean')throw Error('NATIVE_CONTROL_INVALID');return {activeTasks:v.activeTasks,scheduledTasks:v.scheduledTasks};},updateTasks:async action=>{if(!['inspect','lock','unlock'].includes(action))throw Error('NATIVE_UPDATE_ACTION_INVALID');const v=await control('update-tasks',action);if(typeof v.active!=='boolean')throw Error('NATIVE_CONTROL_INVALID');return v.active;},cloudEntryRequired:false,companyPublishAllowed:false};}
 catch(error){await stop();throw error;}
}

/** Reuse the existing PKCE/verification-code IAM flow unchanged. Synthetic tests
 * inject neither a production URL override nor company credentials into native. */
export async function signInAndStartCompanyNativeSession(options){
 const {openBrowser,signal,onStatus=()=>{}}=options;
 if(typeof openBrowser!=='function')throw Error('SYSTEM_BROWSER_CALLBACK_REQUIRED');
 signal?.throwIfAborted();stateDirectory(options.stateRoot);
 const deviceLogin=createCompanyDeviceLogin({directory:ownedDirectory(options.stateRoot,'device-auth'),safeStorage:options.safeStorage,api:companyAuth});
 let session;
 const expiry=companyAuth.createSessionExpiry({onExpire:()=>{void session?.stop();options.onFailure?.(Error('COMPANY_SESSION_EXPIRED'));},renew:companyAuth.renewSession});
 try{
  const auth=await deviceLogin.authenticate({openBrowser,signal,onStatus});
  const {value}=await companyAuth.jsonRequest('/v1/personal-model/models',{token:auth.token,signal});
  const models=value?.data?.filter(x=>x.id==='deepseek-v4.1-flash').map(({id})=>({id}));
  session=await startCompanyNativeSession({...options,auth,models,gatewayOrigin:companyAuth.GATEWAY,testOnly:false});
  expiry.arm(auth);const stop=session.stop;
  session.stop=async()=>{expiry.clear();await deviceLogin.stop();await stop();};
  session.blockResume=async()=>{deviceLogin.blockResume();};
  session.signOut=async()=>{expiry.clear();deviceLogin.blockResume();await stop();try{return await deviceLogin.signOut(auth);}finally{await deviceLogin.stop();}};
  session.deviceStatus=()=>deviceLogin.status();
  return session;
 }catch(error){expiry.clear();await deviceLogin.stop();await session?.stop();throw error;}
}
