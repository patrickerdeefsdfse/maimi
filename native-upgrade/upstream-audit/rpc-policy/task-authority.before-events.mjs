// Two independently authorized root conversations; same-root admission is serial.
const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
export class TaskAuthority {
 constructor({limit=2}={}){if(limit!==2)throw Error('parallel limit invalid');this.limit=limit;this.tasks=new Map();this.revision=0;this.sequence=0;this.generations=new Map();}
 get(session){return this.tasks.get(session)||null;}
 list(){return [...this.tasks.values()];}
 has(task){return !!task&&this.get(task.sessionId)===task;}
 get busy(){return this.tasks.size>0;}
 get modelRequests(){return this.list().reduce((n,t)=>n+t.models,0);}
 get dispatches(){return this.list().reduce((n,t)=>n+t.dispatches,0);}
 reconcile(state){
  if(!state||state.protocol!==2||!Array.isArray(state.activeRoots)||typeof state.busy!=='boolean'
   ||state.activeRoots.some(id=>!SID.test(id)))throw Error('cloud task state unavailable');
  if(state.busy&&!state.activeRoots.length)return;
  for(const task of this.list())if(!state.activeRoots.includes(task.sessionId)&&!task.models&&!task.dispatches&&!task.tickets){
   this.tasks.delete(task.sessionId);this.revision++;
  }
 }
 claim(lease,sessionId,state){
  if(!/^mcl_[A-Za-z0-9_-]{43}$/.test(lease||'')||!SID.test(sessionId||''))throw Error('cloud task identity invalid');
  this.reconcile(state);
  if(this.get(sessionId)||state.activeRoots.includes(sessionId))throw Error('当前对话仍在运行，请等待完成后再发送。');
  if(state.busy&&!state.activeRoots.length||state.activeRoots.some(root=>!this.get(root)))throw Error('云端任务正在结束，请稍后再试。');
  if(this.tasks.size>=this.limit)throw Error('已有两个对话正在运行，请等待其中一个完成。');
  const task={lease,sessionId,rootSessionId:sessionId,revoked:false,generation:++this.sequence,models:0,dispatches:1,tickets:0};
  this.tasks.set(sessionId,task);this.generations.set(sessionId,task.generation);this.revision++;return task;
 }
 hold(task,kind){if(!this.has(task)||!['dispatches','models','tickets'].includes(kind))throw Error('cloud task hold invalid');
  task[kind]++;this.revision++;let done=false;return ()=>{if(done)return;done=true;task[kind]--;this.revision++;};}
 finishDispatch(task){if(!task||task.dispatches<=0)throw Error('cloud dispatch ownership invalid');task.dispatches--;this.revision++;}
 assertMutation(lease,session){
  const tasks=session?[this.get(session)].filter(Boolean):this.list();
  if(tasks.some(task=>task.revoked||task.lease!==lease))throw Error('另一端正在运行当前任务，请等待完成后再操作。');
 }
 resolveScope(scope){const task=this.get(scope?.rootSessionId);
  if(!task||task.revoked||task.generation!==scope.generation||!SID.test(scope.sessionId||''))throw Error('CLOUD_NATIVE_SCOPE_REQUIRED');return task;}
 revoke(task){if(this.has(task)){task.revoked=true;this.revision++;}}
}

// Reviewed against the generated 0.2.1-rc.2 Remote descriptors. Older read
// aliases remain for compatible clients; new names are exact, not verb guesses.
// Read-only calls do not acquire another conversation's task authority.
export const READ_RPC = new Set(['session/list','session/search','session/page','session/attachment','session/projections',
  'workspaceFiles/stat','workspaceFiles/readBytes','officeToPdf/generation',
  'session/modelCatalog','session/canOpenWorkspacePath','session/workspaceDesktop',
  'workspace/list','workspace/files','workspace/stat','settings/get','settings/list','settings/describe',
  'commands/list','agentPreset/list','permissionPreset/list',
  'agentPresets/list','agentPresets/read','permissionPresets/catalog','pluginInventory/list',
  'pluginManager/inspect','pluginManager/listBundles','pluginManager/listPlugins',
  'pluginManager/listVersionExemptions','pluginManager/registries','pluginManager/waitForInstall',
  'pluginMarket/listIndexes','pluginMarket/search','pluginRegistryProbe/fastest',
  'dynamicCordisRunner/getClientCode','dynamicCordisRunner/inventory',
  'schedule/list','schedule/get','schedule/catalog','schedule/history',
  'goal/list','goal/get','goal/catalog','goals/get','workflow/list','workflow/get','workflow/catalog',
  'remoteAccess/follow','session/follow','session/control']);
// The current deployment owns these lifecycle capabilities. An authenticated
// login alone must not start durable work or replace its managed plugins.
// Unknown future methods in these namespaces fail closed until reviewed.
const MANAGED_RPC_NAMESPACES = new Set(['schedule','goal','goals','workflow',
  'pluginManager','pluginMarket','dynamicCordisRunner']);
export function validateNativeRpc(path, method, body, nativeId) {
  if (!path.startsWith('/api/') || method !== 'POST') return null;
  const endpoint = path.slice(5);
  // HTTP endpoint and native envelope must agree; no method smuggling.
  if (!/^[A-Za-z][A-Za-z0-9_-]*\/[A-Za-z][A-Za-z0-9_-]*$/.test(endpoint)
    || body?.type !== 'client-request' || body.method !== endpoint || typeof body.rpcId !== 'string')
    throw Error('cloud native request invalid');
  if (MANAGED_RPC_NAMESPACES.has(endpoint.split('/',1)[0]) && !READ_RPC.has(endpoint)
    || /^cordis\/(?:run|install|enable|update|unload|remove|reload|apply|create)/.test(endpoint))
    throw Error('此操作由公司统一管理，当前云端不允许启动后台任务或变更运行插件。');
  const request = body.payload?.args?.request;
  if (endpoint === 'workspace/create' && request?.path !== '/workspace')
    throw Error('网页版和个人云端只使用本人的云端工作区。');
  if (['workspace/archive','workspace/remove','workspace/delete','workspace/rename'].includes(endpoint))
    throw Error('个人云端工作区不能删除、归档或重命名。');
  if (endpoint === 'session/create' && (request?.cwd != null
    || (request?.workspaceId != null && request.workspaceId !== nativeId)))
    throw Error('只能在本人的云端工作区创建对话。');
  return { endpoint, request, rpcId: body.rpcId, readOnly: READ_RPC.has(endpoint) };
}
