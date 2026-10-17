import test from 'node:test';
import assert from 'node:assert/strict';
import {installEnterprise,createRequestPlan} from './enterprise-native.mjs';
const A='session-4ca46096-09df-40d6-86cf-16e3d333c9f2', B='session-23a46096-09df-40d6-86cf-16e3d333c9f2', C='78a46096-09df-40d6-86cf-16e3d333c9f2';
const uploadId='57164630-a37b-486b-9ae5-c3ce875d2161';
const plugin={plugin_key:'synthetic-summary',version:'1.0.0',name:'文件摘要'};
const completed={status:'completed',run_id:uploadId,conversation_id:C,result:{text:'synthetic result'}};
const nodes=t=>!t||typeof t!=='object'?[]:[t,...(t.children||[]).flatMap(nodes)];
const text=t=>t==null||t===false?'':typeof t!=='object'?String(t):(t.children||[]).map(text).join(' ');
const find=(v,key)=>nodes(v.render()).find(n=>n.props[key]!==undefined);
const flush=()=>new Promise(r=>setImmediate(r));
function harness(t){
 let rendering,source,reloads=0;
 const slots=new Map(),scopes=new Map(),requests=[],confirmations=[],notices=[];
 const sessions={current:A,byId:{}}; const workspaceState={phase:"ready",items:[{workspaceId:C}]},opened=[],connectionState={value:"connected"};
 const React={Fragment:Symbol('fragment'),createElement:(type,props,...children)=>({type,props:props||{},children:children.flat()}),
 useState(initial){const s=rendering,i=s.index++;if(!(i in s.values))s.values[i]=typeof initial==='function'?initial():initial;return[s.values[i],v=>s.values[i]=typeof v==='function'?v(s.values[i]):v];},
 useRef(initial){const s=rendering,i=s.index++;return s.values[i]??={current:initial};},
 useEffect(effect){const s=rendering,i=s.index++;if(!(i in s.values)){s.values[i]=true;s.effects.push(effect);}}};
 const ctx={slots:{inject(_name,f){f();},register(meta,c){slots.set(meta.name,c);}},on(){},get(name){return name==='sessions'?{scope:id=>scopes.get(id)?.actx,list:{getSnapshot:()=>sessions}}:name==='connection'?{state:{getSnapshot:()=>connectionState.value}}:name==='workspaces'?{list:{getSnapshot:()=>workspaceState}}:name==='uiWorkspace'?{openWorkspace:async id=>opened.push(id)}:undefined;},inject(_d,f){f({...this,inputTriggers:{registerSource(v){source=v;}}});}};
 installEnterprise(ctx,()=>React,createRequestPlan);
 const handlers={confirm:()=>true,fetch:async url=>url.endsWith('/desktop/workspace')?{mode:'local'}:url.includes('/plugins/results?')?{items:[]}:url.endsWith('/plugins/uploads')?{attachment_id:uploadId}:url.endsWith('/plugins')?{items:[plugin]}:completed};
 t.mock.method(globalThis,'fetch',async(url,options)=>{const r={url,options,body:options?.body?JSON.parse(options.body):undefined};requests.push(r);const v=await handlers.fetch(url,r);return v instanceof Response?v:new Response(JSON.stringify(v),{status:200});});
 const storage=new Map();
 const old=globalThis.window;globalThis.window={confirm:m=>{confirmations.push(m);return handlers.confirm(m);},location:{reload(){reloads++;}},sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},addEventListener(){},removeEventListener(){}};
 t.after(()=>{if(old===undefined)delete globalThis.window;else globalThis.window=old;});
 function addSession(id=A,file=new File(['fixture text'],'fixture.txt',{type:'text/plain',lastModified:42})){
 const state={draft:'report',draftRev:4,attachmentIds:file?['native-file']:[]},s={state,file};sessions.byId[id]={running:false};
 const facade={state:{getSnapshot:()=>state},beginCommand(claim,span){s.claim=claim;s.span=span;state.draft=claim.token+state.draft.slice(span.end);return true;},setDraft(v){state.draft=v;},notify(level,msg){notices.push({id,level,msg});if(s.notifyThrows)throw Error('disposed');}};
 const conversation={input:{for:()=>facade},resolveDraftAttachments(ids){assert.deepEqual(ids,state.attachmentIds);return s.file?[{id:ids[0],file:s.file}]:[];}};
 s.actx={get:n=>n==='conversation'?conversation:undefined};s.serialized=()=>s.file?[{type:'file',receiptId:'never-forward-native-receipt'}]:[];scopes.set(id,s);return s;}
 function mount(component,props={}){const s={values:[],index:0,effects:[]},clean=[];return{render(){rendering=s;s.index=0;return component(props);},runEffects(){for(const f of s.effects.splice(0)){const c=f();if(c)clean.push(c);}},dispose(){clean.forEach(f=>f());}};}
 function launcher(id=A){const s=scopes.get(id);return mount(slots.get('conversation.input.left'),{sessionId:id,useSession:f=>f({sessionId:id}),useInput:f=>f(s.state)});}
 function dock(name,id=A){const seat=mount(slots.get('conversation.input.dock'),{sessionId:id}).render();const node=nodes(seat).find(n=>n.type?.name===name);return mount(node.type,node.props);}
 function choose(id=A,selection={plugin}){const v=launcher(id);find(v,'data-megmeet-plugin-picker').props.onClick();nodes(v.render()).find(n=>typeof n.type==='function').props.onChoose(selection);return scopes.get(id).claim;}
 return{slots,requests,handlers,confirmations,notices,addSession,choose,launcher,mount,dock,sessions,workspaceState,opened,connectionState,storage,source:()=>source,reloads:()=>reloads,posts:()=>requests.filter(r=>r.options.method==='POST')};
}
test('queued manual cloud entry retries without selecting or reloading before admission',async t=>{
 const h=harness(t),a=h.addSession(A,null);a.state.draft='';let opens=0,release;
 const gate=new Promise(r=>release=r);
 h.handlers.fetch=async url=>{
  if(url.endsWith('/desktop/workspace'))return {mode:'local'};
  if(url.endsWith('/cloud/open')){opens++;return opens===1?{status:'queued',position:2,retry_after:5}:{mode:'cloud'};}
  throw Error('unexpected request');
 };
 const original=globalThis.setTimeout;
 t.mock.method(globalThis,'setTimeout',(fn,ms,...args)=>ms===5000?(gate.then(fn),0):original(fn,ms,...args));
 const v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});
 v.render();v.runEffects();await flush();
 const action=find(v,'data-megmeet-cloud-open').props.onClick();await flush();
 assert.match(text(v.render()),/第 2 位/);assert.equal(opens,1);assert.equal(h.reloads(),0);
 release();await action;assert.equal(opens,2);assert.equal(h.reloads(),1);
});

test('compact selector only lists authorized plugins, creates chip and removes without erasing message',async t=>{
 const h=harness(t),a=h.addSession(),v=h.launcher();find(v,'data-megmeet-plugin-picker').props.onClick();const p=nodes(v.render()).find(n=>typeof n.type==='function'),popup=h.mount(p.type,p.props);popup.render();popup.runEffects();await flush();
 const tree=popup.render();assert.equal(tree.props.role,'menu');assert.equal(nodes(tree).some(n=>n.type==='select'||n.props.role==='dialog'),false);
 nodes(tree).find(n=>n.props['data-megmeet-plugin-key']==='synthetic-summary').props.onClick();assert.equal(a.state.draft,'/企业任务 report');assert.equal(a.claim.hint,'文件摘要');
 assert.equal(find(v,'data-megmeet-plugin-chip').props['data-megmeet-plugin-chip'],'synthetic-summary');nodes(v.render()).find(n=>n.props['aria-label']==='移除所选插件').props.onClick();assert.equal(a.state.draft,'report');assert.equal(h.posts().length,0);assert.equal(h.slots.has('sidebar.footer.action'),false);
});
test('late plugin catalog response after disposal is ignored',async t=>{
 const h=harness(t);h.addSession();const v=h.launcher();find(v,'data-megmeet-plugin-picker').props.onClick();const p=nodes(v.render()).find(n=>typeof n.type==='function'),popup=h.mount(p.type,p.props);let release;h.handlers.fetch=()=>new Promise(r=>release=r);popup.render();popup.runEffects();popup.dispose();release({items:[plugin]});await flush();assert.equal(text(popup.render()).includes('文件摘要'),false);
});
test('cloud recycle bin queues only the selected session without entering cloud or exposing owner',async t=>{
 const h=harness(t),pinned=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});
 const seat=nodes(pinned.render()).find(n=>typeof n.type==='function'&&n.type.name==='HistoryTrash');
 assert.ok(seat);const popup=h.mount(seat.type,seat.props);
 h.handlers.fetch=async url=>url.endsWith('/history/trash')?{items:[{session_id:A,title:'Synthetic old chat',purge_after:'2099-01-01T00:00:00Z',restore_requested_at:null}]}:{status:'queued'};
 nodes(popup.render()).find(n=>n.type==='button').props.onClick();await flush();
 assert.match(text(popup.render()),/Synthetic old chat/);
 await nodes(popup.render()).find(n=>n.type==='button'&&text(n)==='恢复').props.onClick();
 assert.match(text(popup.render()),/等待恢复/);assert.equal(h.reloads(),0);
 assert.deepEqual(h.posts().map(r=>r.body),[{session_id:A}]);
 assert.equal(h.requests.some(r=>r.url.includes('/cloud/open')),false);
});
test('cancelled task uploads nothing and retains draft/file',async t=>{
 const h=harness(t),a=h.addSession(),claim=h.choose();h.handlers.confirm=()=>false;assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'error');assert.equal(h.posts().length,0);assert.equal(a.file.name,'fixture.txt');assert.match(a.state.draft,/report/);
});
test('explicit original File upload, no native receipt/history forwarded',async t=>{
 const h=harness(t),a=h.addSession(),claim=h.choose();assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'success');const sent=h.posts();assert.equal(sent.length,2);assert.equal(sent[0].url.endsWith('/plugins/uploads'),true);assert.deepEqual(sent[1].body.attachment_ids,[uploadId]);assert.equal(sent[1].body.native_session_id,A);assert.equal(sent[1].body.location,'local');assert.equal(JSON.stringify(sent).includes('never-forward-native-receipt'),false);assert.equal('workspace_id'in sent[1].body,false);assert.match(h.confirmations[0],/服务器/);
});
test('session does not inherit another plugin; replacement invalidates stale claim',async t=>{
 const h=harness(t),a=h.addSession(),b=h.addSession(B),claim=h.choose();assert.equal((await h.source().matchEnter({sessionId:B},'/企业任务 report').claim.submit('report',b.actx,b.serialized())).kind,'error');h.choose(A,{plugin:{...plugin,version:'2.0.0'}});assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'error');assert.equal(h.posts().length,0);
});
test('network retry reuses upload/request ID without reconfirming',async t=>{
 const h=harness(t),a=h.addSession(),claim=h.choose(),normal=h.handlers.fetch;let count=0;h.handlers.fetch=async(u,r)=>{if(u.endsWith('/tasks')&&++count===1)throw Error('lost response');return normal(u,r);};assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'error');assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'success');assert.equal(h.posts().filter(r=>r.url.endsWith('/uploads')).length,1);const ts=h.posts().filter(r=>r.url.endsWith('/tasks'));assert.deepEqual(ts[0].body,ts[1].body);assert.equal(h.confirmations.length,1);
});
test('running retry reuses same task; disposed notice cannot undo completed operation',async t=>{
 const h=harness(t),a=h.addSession(),claim=h.choose(),normal=h.handlers.fetch;let count=0;h.handlers.fetch=async(u,r)=>u.endsWith('/tasks')&&++count===1?{status:'running'}:normal(u,r);assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'error');a.notifyThrows=true;assert.equal((await claim.submit('report',a.actx,a.serialized())).kind,'success');const ts=h.posts().filter(r=>r.url.endsWith('/tasks'));assert.equal(ts[0].body.request_id,ts[1].body.request_id);
});
test('upload permission loss stops execution',async t=>{
 const h=harness(t),a=h.addSession(),claim=h.choose(),normal=h.handlers.fetch;h.handlers.fetch=async(u,r)=>u.endsWith('/uploads')?new Response('',{status:403}):normal(u,r);const v=await claim.submit('report',a.actx,a.serialized());assert.equal(v.kind,'error');assert.match(v.text,/权限/);assert.equal(h.posts().length,1);
});
test('continuation uses matching authorized plugin/version association',async t=>{
 const h=harness(t),a=h.addSession(A,null),claim=h.choose(),normal=h.handlers.fetch;h.handlers.fetch=async(u,r)=>u.includes('/plugins/results?')?{items:[{...completed,plugin_key:plugin.plugin_key,plugin_version:plugin.version},{...completed,conversation_id:B,plugin_key:'other'}]}:normal(u,r);assert.equal((await claim.submit('continue',a.actx,[])).kind,'success');assert.equal(h.posts()[0].body.conversation_id,C);
});
test('result card reads current native session/mode and discloses server storage',async t=>{
 const h=harness(t),normal=h.handlers.fetch;h.handlers.fetch=async(u,r)=>u.includes('/plugins/results?')?{items:[{...completed,input:'only this session',plugin_name:'文件摘要'}]}:normal(u,r);const card=h.dock('PluginResults');card.render();card.runEffects();await flush();assert.match(text(card.render()),/only this session/);assert.match(text(card.render()),/保存在服务器/);assert.equal(h.requests.find(r=>r.url.includes('/plugins/results?')).url.endsWith(`native_session_id=${A}&location=local`),true);
});
test('cloud pinned row remains offline, has no remove control and never auto-opens',async t=>{
 const h=harness(t);h.handlers.fetch=async()=>{throw Error('offline');};const v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});v.render();v.runEffects();await flush();assert.match(text(v.render()),/云端工作区/);assert.equal(nodes(v.render()).filter(n=>n.type==='button').length,1);assert.equal(h.posts().length,0);assert.equal(h.reloads(),0);
});
test('cloud only opens on explicit click, duplicate click cannot open twice',async t=>{
 const h=harness(t),v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true}),normal=h.handlers.fetch;h.handlers.fetch=async(u,r)=>u.endsWith('/cloud/open')?{mode:'cloud'}:normal(u,r);v.render();v.runEffects();await flush();const click=find(v,'data-megmeet-cloud-open').props.onClick;await Promise.all([click(),click()]);assert.equal(h.posts().length,1);assert.equal(h.reloads(),1);assert.equal(h.posts()[0].url,'/__megmeet/desktop/cloud/open');
});
test('running or unsent draft blocks switch; cloud offers explicit return local',async t=>{
 const h=harness(t),a=h.addSession(),v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});h.sessions.byId[A].running=true;await find(v,'data-megmeet-cloud-open').props.onClick();assert.match(text(v.render()),/任务完成/);h.sessions.byId[A].running=false;await find(v,'data-megmeet-cloud-open').props.onClick();assert.match(text(v.render()),/草稿/);assert.equal(h.posts().length,0);const cloud=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});h.handlers.fetch=async u=>u.endsWith('/local/open')?{mode:'local'}:{mode:'cloud'};a.state.draft='';a.state.attachmentIds=[];cloud.render();cloud.runEffects();await flush();await find(cloud,'data-megmeet-local-open').props.onClick();assert.equal(h.reloads(),1);
});
test('model sync is automatic, has no button and preserves active draft without reload',async t=>{
 const h=harness(t),a=h.addSession(A,null),normal=h.handlers.fetch;
 const old=globalThis.document;globalThis.document={visibilityState:'visible',addEventListener(){},removeEventListener(){}};
 t.after(()=>{if(old===undefined)delete globalThis.document;else globalThis.document=old;});
 h.handlers.fetch=async(u,r)=>u.endsWith('/models/refresh')?{notice:'首选模型尚未获授权',native_reload_required:false}:normal(u,r);
 const v=h.mount(h.slots.get('conversation.input.right'));v.render();v.runEffects();await flush();assert.match(text(v.render()),/尚未获授权/);
 assert.equal(nodes(v.render()).some(n=>n.type==='button'),false);assert.equal(h.posts().length,1);
 assert.equal(a.state.draft,'report');assert.equal(h.reloads(),0);v.dispose();
});

test('cloud pinned row explicitly opens sole native workspace after archive without booting again',async t=>{
 const h=harness(t);h.handlers.fetch=async()=>({mode:'cloud'});const v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});v.render();v.runEffects();await flush();
 assert.deepEqual(h.opened,[]);await find(v,'data-megmeet-cloud-open').props.onClick();assert.deepEqual(h.opened,[C]);assert.equal(h.posts().length,0);assert.equal(h.reloads(),0);
 h.workspaceState.items.push({workspaceId:B});await find(v,'data-megmeet-cloud-open').props.onClick();assert.deepEqual(h.opened,[C]);assert.match(text(v.render()),/状态异常/);
});
test('ordinary native conversation remains clean if plugin history service is unavailable',async t=>{
 const h=harness(t),normal=h.handlers.fetch;h.handlers.fetch=async(u,r)=>u.includes('/plugins/results?')?new Response('',{status:503}):normal(u,r);
 const v=h.dock('PluginResults');v.render();v.runEffects();await flush();assert.equal(v.render(),null);
});

test('manual cloud click reconnects lost native transport without an automatic worker launch',async t=>{
 const h=harness(t);h.handlers.fetch=async()=>({mode:'cloud'});h.connectionState.value='disconnected';
 const v=h.mount(h.slots.get('sidebar.workspaces.pinned'),{wide:true});v.render();v.runEffects();await flush();assert.equal(h.posts().length,0);
 await find(v,'data-megmeet-cloud-open').props.onClick();assert.equal(h.posts()[0].url,'/__megmeet/desktop/cloud/open');assert.equal(h.reloads(),1);assert.deepEqual(h.opened,[]);
});

test('one manual cloud click opens sole native workspace after reload exactly once',async t=>{
 const h=harness(t),local=h.mount(h.slots.get('sidebar.workspaces.pinned'));
 h.handlers.fetch=async u=>({mode:u.endsWith('/cloud/open')?'cloud':'local'});
 local.render();local.runEffects();await flush();await find(local,'data-megmeet-cloud-open').props.onClick();local.dispose();
 assert.equal(h.reloads(),1);assert.equal(h.storage.size,1);assert.deepEqual(h.opened,[]);
 h.handlers.fetch=async()=>({mode:'cloud'});
 const cloud=h.mount(h.slots.get('sidebar.workspaces.pinned'));cloud.render();cloud.runEffects();await flush();
 assert.deepEqual(h.opened,[C]);assert.equal(h.storage.size,0);assert.equal(h.posts().length,1);cloud.dispose();
 const refreshed=h.mount(h.slots.get('sidebar.workspaces.pinned'));refreshed.render();refreshed.runEffects();await flush();
 assert.deepEqual(h.opened,[C]);assert.equal(h.posts().length,1);refreshed.dispose();
});

test('expired intent or a fresh local launch cannot select a cloud workspace',async t=>{
 const h=harness(t);h.storage.set('megmeet.explicit-cloud-open.v1',String(Date.now()-180000));
 h.handlers.fetch=async()=>({mode:'cloud'});const expired=h.mount(h.slots.get('sidebar.workspaces.pinned'));expired.render();expired.runEffects();await flush();assert.deepEqual(h.opened,[]);assert.equal(h.storage.size,0);expired.dispose();
 h.storage.set('megmeet.explicit-cloud-open.v1',String(Date.now()));h.handlers.fetch=async()=>({mode:'local'});
 const fresh=h.mount(h.slots.get('sidebar.workspaces.pinned'));fresh.render();fresh.runEffects();await flush();assert.deepEqual(h.opened,[]);assert.equal(h.storage.size,0);assert.equal(h.posts().length,0);fresh.dispose();
});
