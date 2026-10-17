import { randomUUID } from 'node:crypto';
import { PROJECT_KEYS } from './project-bridge.mjs';

export const PROTOCOL = 'megmeet-connectors-v1';
const SESSION = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const fail = (code, status = 400) => { throw Object.assign(Error(code), { code, status }); };
const plain = x => x && typeof x === 'object' && !Array.isArray(x);

// One instance per employee Host. Client identity is injected by the trusted
// wrapper, never accepted from tool arguments or browser identity headers.
export function createToolkit({ remote, local, authorize = async () => {}, now = Date.now, projects } = {}) {
  let revisionSequence = 0;
  const selections = new Map(), pending = new Map(), active = new Map(), changes = new Map();
  let closed = false;
  const key = (session, context) => {
    if (closed) fail('TOOLKIT_CLOSED', 401);
    if (!SESSION.test(session || '') || !context?.clientKey || typeof context.clientKey !== 'string') fail('TOOLKIT_SESSION_REQUIRED');
    return context.clientKey + ':' + session;
  };

  // Private host API for native web_search / web_fetch. Not exposed by the
  // enterprise model-call schema or the browser selection/control endpoint.
  const webActive = new Map();
  const stopWeb = scope => { for (const [controller, owner] of webActive) if (scope === undefined || owner === scope) controller.abort(); };
  async function nativeWeb(body, context) {
    await authorize(context);
    const scope = key(body?.session_id, context);
    if (!plain(body) || Object.keys(body).sort().join(',') !== 'action,arguments,session_id' || !plain(body.arguments)) fail('WEB_ARGUMENTS_INVALID');
    const args = body.arguments;
    if (body.action === 'web_search') {
      if (Object.keys(args).sort().join(',') !== 'count,query' || typeof args.query !== 'string' || !args.query.trim() || args.query.length > 400 || !Number.isSafeInteger(args.count) || args.count < 1 || args.count > 10) fail('WEB_ARGUMENTS_INVALID');
    } else if (body.action === 'web_read') {
      if (Object.keys(args).join(',') !== 'url' || typeof args.url !== 'string' || args.url.length > 2048) fail('WEB_ARGUMENTS_INVALID');
      let url; try { url = new URL(args.url); } catch { fail('WEB_ARGUMENTS_INVALID'); }
      if (url.protocol !== 'https:' || url.username || url.password) fail('WEB_ARGUMENTS_INVALID');
    } else fail('WEB_ARGUMENTS_INVALID');
    if (webActive.size >= 8) fail('WEB_BUSY', 429);
    if (closed || context.signal?.aborted) fail('WEB_CANCELLED', 409);
    const controller = new AbortController(); webActive.set(controller, scope);
    const signal = AbortSignal.any([context.signal, controller.signal, AbortSignal.timeout(85000)].filter(Boolean));
    try {
      signal.throwIfAborted(); await authorize(context); signal.throwIfAborted();
      const value = await remote('/invoke', { connector: 'web-search', action: body.action, arguments: args, request_id: randomUUID() }, { ...context, signal });
      signal.throwIfAborted(); await authorize(context); signal.throwIfAborted();
      if (closed) fail('WEB_CANCELLED', 409);
      return value;
    } finally { webActive.delete(controller); }
  }

  async function catalog(context) {
    await authorize(context);
    let items = [], unavailable = false;
    try { items = (await remote('/catalog', undefined, context)).items || []; }
    catch { unavailable = true; }
    if (!Array.isArray(items) || items.length > 32) fail('CONNECTOR_CATALOG_INVALID', 503);
    items = items.filter(item => item.plugin_key !== 'web-search');
    if (projects && SESSION.test(context?.native_session_id || '')) {
      items = items.filter(item => !PROJECT_KEYS.has(item.plugin_key));
      try {
        const projected = await projects.read('catalog', context.native_session_id, context);
        if (!Array.isArray(projected.items) || projected.items.length > 4) fail('PROJECT_CATALOG_INVALID', 503);
        items.push(...projected.items.filter(item => PROJECT_KEYS.has(item.plugin_key)).map(item => ({ ...item, tools: (item.tools || []).map(tool => ({ ...tool, model_tool: 'enterprise_project_call' })) })));
      } catch { unavailable = true; } // Ordinary selected skills remain usable; missing project actions fail closed.
    }
    return { protocol: PROTOCOL, items: [...items, ...(local?.catalog() || [])], unavailable };
  }
  function prune() {
    for (const [id, item] of pending) if (item.deadline <= now()) pending.delete(id);
    for (const [id, item] of selections) if (item.deadline <= now()) { void projects?.revokeScope(item.session, { clientKey: item.clientKey }).catch(() => {}); selections.delete(id); active.get(id)?.abort(); }
  }
  async function control(action, body = {}, context) {
    await authorize(context); prune();
    if (action === 'catalog') return catalog({ ...context, native_session_id: body.session_id });
    const scope = key(body.session_id, context);
    if (action === 'state') {
      const query = {}; const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
      if(body.history_job_ids!=null){if(!Array.isArray(body.history_job_ids)||!body.history_job_ids.length||body.history_job_ids.length>16||body.history_job_ids.some(id=>!uuid.test(id)))fail('PROJECT_HISTORY_QUERY_INVALID');query.history_job_ids=body.history_job_ids;}
      for(const name of ['history_before','history_children_of'])if(body[name]!=null){if(!uuid.test(body[name]))fail('PROJECT_HISTORY_QUERY_INVALID');query[name]=body[name];}
      if(body.history_limit!=null){if(!Number.isInteger(body.history_limit)||body.history_limit<1||body.history_limit>50)fail('PROJECT_HISTORY_QUERY_INVALID');query.history_limit=body.history_limit;}
      if(query.history_job_ids&&(query.history_before||query.history_children_of))fail('PROJECT_HISTORY_QUERY_INVALID');
      const projection = projects ? await projects.read('results', body.session_id, context, query) : {};
      return { selected: selections.get(scope)?.ids || [], pending: [...pending.values()].filter(p => p.scope === scope).map(p => p.public), results: projection.results || [], jobs: projection.jobs || [], history:projection.history||[], history_next_cursor:projection.history_next_cursor||null, history_remaining_job_ids:projection.history_remaining_job_ids||[] };
    }
    if (['invoke','cancel-job','form-ticket','secure-form'].includes(action)) {
      if (!projects) fail('PROJECT_CLOUD_REQUIRED', 409);
      return projects.control(action, body, context);
    }
    if (action === 'selection') {
      if (!Array.isArray(body.plugins) || body.plugins.length > 4 || new Set(body.plugins).size !== body.plugins.length) fail('PLUGIN_SELECTION_INVALID');
      if (changes.has(scope)) fail('TOOL_CONTROL_BUSY', 409);
      if (!selections.has(scope) && selections.size + changes.size >= 512) fail('TOO_MANY_TOOL_SESSIONS', 429);
      const change = Symbol(); changes.set(scope, change);
      const enabled = [];
      const stillCurrent = () => { if (closed || changes.get(scope) !== change) fail('PLUGIN_SELECTION_CHANGED', 409); };
      try {
      const available = await catalog({ ...context, native_session_id: body.session_id });
      stillCurrent();
      for (const id of body.plugins) {
        const item = available.items.find(p => p.plugin_key === id);
        if (!item || !['ready', 'available', 'limited'].includes(item.status)) fail('PLUGIN_NOT_READY', 409);
      }
      for (const id of body.plugins) {
        if (local?.owns(id) && !selections.get(scope)?.ids.includes(id)) {
          await local.enable(id, scope);
          enabled.push(id);
        }
        stillCurrent();
      }
      if (!selections.has(scope) && selections.size >= 512) fail('TOO_MANY_TOOL_SESSIONS', 429);
      active.get(scope)?.abort();
      await projects?.revokeScope(body.session_id, context);
      for (const id of selections.get(scope)?.ids || []) if (!body.plugins.includes(id)) await local?.disable(id, scope);
      stillCurrent();
      selections.set(scope, { ids: [...body.plugins], revision: ++revisionSequence, deadline: now() + 30 * 60_000, session: body.session_id, clientKey: context.clientKey });
      for (const [id,p] of pending) if (p.scope === scope) pending.delete(id);
      return { selected: [...body.plugins] };
      } catch (error) {
        // A rejected multi-plugin change must not leave the earlier grants active.
        // A stop already revokes them; do not touch a newer selection after it.
        if (changes.get(scope) === change) for (const id of enabled) await local.disable(id, scope);
        throw error;
      } finally { if (changes.get(scope) === change) changes.delete(scope); }
    }
    if (action === 'stop') {
      stopWeb(scope);
      changes.delete(scope);
      active.get(scope)?.abort();
      await projects?.revokeScope(body.session_id, context); selections.delete(scope);
      for (const [id,p] of pending) if (p.scope === scope) pending.delete(id);
      await local?.stop(scope);
      return { selected: [], stopped: true };
    }
    if (action === 'confirm') {
      const proposal = pending.get(body.confirmation_id);
      if (!proposal || proposal.scope !== scope || proposal.deadline <= now()) fail('CONFIRMATION_EXPIRED', 409);
      pending.delete(body.confirmation_id);
      if (body.confirmed !== true) return { status: 'cancelled' };
      if (!selections.get(scope)?.ids.includes(proposal.connector)) fail('PLUGIN_NOT_SELECTED', 403);
      if (active.has(scope)) fail('TOOL_BUSY', 409);
      // This route is only exposed to authenticated user UI. It is deliberately
      // absent from the model tool schema and from execute() below.
      const abort = new AbortController(); active.set(scope, abort);
      try {
        const signal = AbortSignal.any([context.signal, abort.signal].filter(Boolean));
        const value = await remote('/confirm', { confirmation_id: body.confirmation_id, confirmed: true }, {...context,signal});
        signal.throwIfAborted(); await authorize(context);
        return value;
      } finally { if (active.get(scope) === abort) active.delete(scope); }
    }
    fail('TOOL_CONTROL_UNKNOWN', 404);
  }
  async function execute(body, context) {
    await authorize(context); prune();
    const scope = key(body?.session_id, context), selected = selections.get(scope);
    if (['project-invoke','project-attachments'].includes(body.operation) && !projects) fail('PROJECT_CLOUD_REQUIRED: 请切换到云端工作区使用项目技能。',409);
    if (!selected?.ids.length) fail('PLUGIN_NOT_SELECTED: 请先在本条会话的插件菜单中选择功能。', 403);
    const available = await catalog({ ...context, native_session_id: body.session_id });
    // The catalog is remote: stop, expiry or a selection change may occur while
    // it is loading. Recheck before dispatch, not only after the side effect.
    if (closed || selections.get(scope) !== selected || selected.deadline <= now() || context.signal?.aborted) fail('PLUGIN_SELECTION_CHANGED', 409);
    if (body.operation === 'project-attachments') { if (!projects || !selected.ids.some(id => PROJECT_KEYS.has(id))) fail('PROJECT_NOT_SELECTED', 403); return projects.read('attachments', body.session_id, context); }
    if (body.operation === 'catalog') return { protocol: PROTOCOL, items: available.items.filter(p => selected.ids.includes(p.plugin_key)).map(p=>({...p,tools:p.tools.filter(t=>t.available!==false)})) };
    if (!['invoke','project-invoke'].includes(body.operation) || !selected.ids.includes(body.connector) || !plain(body.arguments || {})) fail('PLUGIN_NOT_SELECTED', 403);
    if (JSON.stringify(body.arguments || {}).length > 24_000) fail('TOOL_ARGUMENTS_TOO_LARGE', 413);
    const item = available.items.find(p => p.plugin_key === body.connector);
    if (!item || !['ready', 'available', 'limited'].includes(item.status) || !item.tools?.some(t => t.name === body.action && t.available!==false)) fail('TOOL_NOT_AVAILABLE', 409);
    const projectCall = PROJECT_KEYS.has(body.connector);
    if (projectCall !== (body.operation === 'project-invoke') || (projectCall && !projects)) fail('PROJECT_TOOL_REQUIRED', 400);
    if (active.has(scope)) fail('TOOL_BUSY', 409);
    const abort = new AbortController(); active.set(scope, abort);
    try {
      const callContext = { ...context, signal: context.signal ? AbortSignal.any([context.signal, abort.signal]) : abort.signal };
      const value = projectCall ? await projects.start(body, callContext) : local?.owns(body.connector)
        ? await local.invoke(body.connector, body.action, body.arguments || {}, scope, callContext.signal)
        : await remote('/invoke', { connector: body.connector, action: body.action, arguments: body.arguments || {}, request_id: randomUUID() }, callContext);
      callContext.signal.throwIfAborted(); await authorize(context);
      if (selections.get(scope) !== selected) fail('PLUGIN_SELECTION_CHANGED', 409);
      if (value?.status === 'requires_confirmation') {
        if (typeof value.confirmation_id !== 'string' || value.confirmation_id.length > 128) fail('CONFIRMATION_INVALID', 502);
        for (const [id,p] of pending) if(p.scope===scope) pending.delete(id);
        pending.set(value.confirmation_id, { scope, connector: body.connector, deadline: now() + 300_000,
          public: { ...value, plugin_name: item.name } });
        return { status: 'requires_confirmation', message: '请用户在聊天框上方查看操作内容并点击确认。模型不能代替用户确认。', preview: value.preview };
      }
      return value;
    } finally { if (active.get(scope) === abort) active.delete(scope); }
  }
  async function close() { closed = true; stopWeb(); await projects?.close(); changes.clear(); for (const a of active.values()) a.abort(); selections.clear(); pending.clear(); await local?.close(); }
  async function stopAll(){stopWeb();await projects?.revokeAll();for(const a of active.values())a.abort();const scopes=[...new Set([...selections.keys(),...changes.keys()])];changes.clear();selections.clear();pending.clear();await Promise.all(scopes.map(s=>local?.stop(s)));}
  function snapshot(session, context) {
    const scope = key(session, context), selected = selections.get(scope);
    return { ids: selected && selected.deadline > now() ? [...selected.ids] : [], revision: selected && selected.deadline > now() ? selected.revision : 0 };
  }
  function cancelSession(session, context) { const scope=key(session,context);active.get(scope)?.abort();stopWeb(scope); }
  return { control, execute, nativeWeb, close, catalog, stopAll, snapshot, cancelSession };
}
