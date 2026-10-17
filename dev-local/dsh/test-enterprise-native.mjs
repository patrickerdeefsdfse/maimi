import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequestPlan, installEnterprise } from './enterprise-native.mjs';
import { adaptPinnedWorkspace } from './workspace-pinned-overlay.mjs';
const requestId = 'dfd988b4-edde-44c6-bc2e-46801bd08e2d';
const sessionId = 'session-4ca46096-09df-40d6-86cf-16e3d333c9f2';
const selection = {plugin:{plugin_key:'synthetic-summary',version:'1.0.0'}};
test('plugin request binds selected version to uploads and current native session, without any workspace or owner input',()=>{
 const p=createRequestPlan({...selection,workspace_id:'ignored',owner:'ignored',document_refs:['ignored']},'question',[],requestId,sessionId,'local');
 assert.equal(p.endpoint,'/plugins/tasks');
 assert.equal(p.body.native_session_id,sessionId);assert.equal(p.body.location,'local');
 assert.equal(p.scope.plugin_key,p.body.plugin_key);assert.equal(p.scope.plugin_version,p.body.plugin_version);
 assert.equal(p.scope.explicit_upload,true);assert.equal(p.scope.request_id,p.body.request_id);
 for(const key of ['workspace_id','owner','user_no','document_refs']) assert.equal(key in p.body,false);
});
test('plugin continuation uses only validated server conversation UUID',()=>{
 const p=createRequestPlan(selection,'question',[],requestId,sessionId,'cloud',requestId);
 assert.equal(p.body.conversation_id,requestId);
 assert.throws(()=>createRequestPlan(selection,'q',[],requestId,sessionId,'local','not-uuid'));
});
test('no plugin or invalid session/mode/input/files stops before upload',()=>{
 for(const s of [null,{}, {plugin:{plugin_key:'../all',version:'1'}}]) assert.throws(()=>createRequestPlan(s,'q',[],requestId,sessionId,'local'));
 for(const mode of ['department','group','unknown']) assert.throws(()=>createRequestPlan(selection,'q',[],requestId,sessionId,mode));
 for(const input of ['  ','x'.repeat(20001)]) assert.throws(()=>createRequestPlan(selection,input,[],requestId,sessionId,'local'));
 for(const files of [[{size:262145}],Array(17).fill({size:1})]) assert.throws(()=>createRequestPlan(selection,'q',files,requestId,sessionId,'local'));
 assert.throws(()=>createRequestPlan(selection,'q',[],requestId,'native-A','local'));
});
test('unbound plugin command is rejected; ordinary message has no enterprise interception',async()=>{
 let source;const ctx={slots:{inject(){},register(){}},on(){},get(){},inject(_deps,fn){fn({...this,inputTriggers:{registerSource(x){source=x;}}});}};
 installEnterprise(ctx,()=>({}),createRequestPlan);
 assert.equal((await source.matchEnter({sessionId},'/企业任务 report').claim.submit('report',{},[])).kind,'error');
 assert.equal(source.matchEnter({sessionId},'ordinary text'),undefined);
});
test('native pinned seam preserves original navigation and refuses an unreviewed upstream',()=>{
 const root=process.env.MEGMEET_CANDIDATE_RUNTIME
   ? pathToFileURL(path.resolve(process.env.MEGMEET_CANDIDATE_RUNTIME)+path.sep)
   : new URL('../../desktop-local/runtime/',import.meta.url);
 let source;
 try{source=readFileSync(new URL('pristine/dsh-client-ui-workspace.js',root),'utf8');}
 catch{source=readFileSync(new URL('node_modules/@deepseek-ai/dsh-client-ui-workspace/lib/client.js',root),'utf8');}
 const updated=adaptPinnedWorkspace(source);
 assert.equal(updated.includes('renderSlot("sidebar.workspaces.pinned", { wide, expandSidebar })'),true);
 assert.equal(updated.includes('startSession(workspaceId)'),true);
 assert.throws(()=>adaptPinnedWorkspace(source+'\n'),/Review/);
});

test('actual DSH generated session prefix is preserved, never confused with explicit UUID session',()=>{
 const generated=createRequestPlan(selection,'q',[],requestId,sessionId,'local');
 const explicit=createRequestPlan(selection,'q',[],requestId,sessionId.slice(8),'local');
 assert.notEqual(generated.body.native_session_id,explicit.body.native_session_id);
 assert.equal(generated.body.native_session_id,sessionId);
});
