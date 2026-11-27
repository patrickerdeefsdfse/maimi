import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {existsSync} from 'node:fs';
import {createWorkspaceBackend} from './workspace-backend.mjs';
const require=createRequire('/opt/dsh/package.json');
const {storageBackendServiceKey}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-storage')).href);
export const name='maimi-pg-workspace';
export const inject=['storage'];
const ENDPOINT='http://127.0.0.1:3086/__megmeet/workspace-state/';
export function transport(token){
 if(!/^[A-Za-z0-9_-]{43}$/.test(token||''))throw Error('WORKSPACE_TOKEN_INVALID');
 async function request(operation,body){
  const response=await fetch(ENDPOINT+operation,{method:'POST',redirect:'error',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(14000)});
  let length=0;const chunks=[];for await(const chunk of response.body){length+=chunk.length;if(length>4*1024**2+4096)throw Error('WORKSPACE_RESPONSE_LIMIT');chunks.push(chunk);}
  const data=JSON.parse(Buffer.concat(chunks).toString());
  if(!response.ok||data.unit!=='workspace'||data.format_version!==2)throw Error('WORKSPACE_REQUEST_REFUSED');
  if(operation==='write'&&data.write_id!==body.write_id)throw Error('WORKSPACE_ACK_ID_MISMATCH');
  return data;
 }
 // A network error after POST is an unknown outcome, never an automatic retry.
 return {load:()=>request('load',{}),compareAndSwap:body=>request('write',body)};
}
export function apply(ctx){
 if(process.env.MAIMI_WORKSPACE_STATE_BACKEND!=='postgres'||typeof process.send!=='function')throw Error('WORKSPACE_HOST_CHANNEL_REQUIRED');
 if(existsSync('/state/dsh/storages/workspace.json'))throw Error('WORKSPACE_LOCAL_IMPORT_REQUIRED');
 const health={ready:true,reason:null};
 const backend=createWorkspaceBackend(transport(process.env.MEGMEET_MODEL_TOKEN),{onFatal:()=>{
  health.ready=false;health.reason='workspace_state_uncertain_or_fenced';
  // Backend already permanently rejects all future writes before notifying Host.
  process.send({type:'maimi-workspace-state-fatal',protocol:1,reason:health.reason},error=>{if(error)process.exitCode=78;});
 }});
 const unregister=ctx.storage.backend.register('maimi-pg',backend);
 ctx.provide(storageBackendServiceKey('maimi-pg'),backend);
 ctx.provide('maimi.workspace-state-health',health);
 ctx.effect(()=>async()=>{unregister();await backend.close();});
}
