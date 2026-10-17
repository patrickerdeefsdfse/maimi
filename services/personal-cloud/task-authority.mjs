// One generating cloud conversation per employee Host. The lease belongs to
// the initiating login, never to whichever browser most recently connected.
export class TaskAuthority {
  constructor() { this.current = null; this.modelRequests = 0; this.dispatches = 0; this.revision = 0; this.sequence = 0; }
  reconcile(state) {
    if (!state || state.protocol !== 1 || !Array.isArray(state.activeRoots)
      || typeof state.busy !== 'boolean') throw Error('cloud task state unavailable');
    if (this.current && !state.busy && !this.modelRequests && !this.dispatches) { this.current = null; this.revision++; }
    return this.current;
  }
  claim(lease, sessionId, state) {
    if (!/^mcl_[A-Za-z0-9_-]{43}$/.test(lease || '')
      || !/^(?:session-)?[0-9a-f-]{36}$/i.test(sessionId || '')) throw Error('cloud task identity invalid');
    this.reconcile(state);
    if (this.current && (this.current.lease !== lease || this.current.sessionId !== sessionId))
      throw Error('另一端或另一段对话正在运行，请等待完成后再发送。');
    if (!this.current && state.busy) throw Error('云端任务正在结束，请稍后再试。');
    if (!this.current) this.current = { lease, sessionId, revoked: false, generation: ++this.sequence };
    if (this.current.revoked) throw Error('发起端登录已失效，请重新登录。');
    this.dispatches++; this.revision++;
    return this.current;
  }
  finishDispatch() { this.dispatches = Math.max(0, this.dispatches - 1); this.revision++; }
  assertMutation(lease) {
    if (this.current && (this.current.revoked || this.current.lease !== lease))
      throw Error('另一端正在运行云端任务，请等待完成后再操作。');
  }
  beginModel(state) {
    const task = this.current;
    if (!task || task.revoked || state.protocol !== 1 || !state.busy
      || state.activeRoots.length !== 1 || state.activeRoots[0] !== task.sessionId) throw Error('cloud task authorization required');
    this.modelRequests++; this.revision++;
    let ended = false;
    return { task, end: () => { if (!ended) { ended = true; this.modelRequests--; this.revision++; } } };
  }
  revoke(task) { if (this.current === task) task.revoked = true; }
}

// Read-only unary calls do not acquire task authority. Everything else remains
// client-bound while a task is active; prompt is the sole generation initiator.
export const READ_RPC = new Set(['session/list','session/search','session/page','session/attachment',
  'workspaceFiles/stat','workspaceFiles/readBytes','officeToPdf/generation',
  'session/modelCatalog','session/canOpenWorkspacePath','session/workspaceDesktop',
  'workspace/list','workspace/files','workspace/stat','settings/get','settings/list',
  'commands/list','agentPreset/list','permissionPreset/list']);
export function validateNativeRpc(path, method, body, nativeId) {
  if (!path.startsWith('/api/') || method !== 'POST') return null;
  const endpoint = path.slice(5);
  // HTTP endpoint and native envelope must agree; no method smuggling.
  if (body?.type !== 'client-request' || body.method !== endpoint || typeof body.rpcId !== 'string')
    throw Error('cloud native request invalid');
  if (/^(?:schedule|goal|workflow)\/(?!list$|get$|catalog$)/.test(endpoint)
    || /^cordis\/(?:run|install|enable|update|unload|remove|reload|apply|create)/.test(endpoint))
    throw Error('云端暂不支持跨登录的定时或自主后台任务。');
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
