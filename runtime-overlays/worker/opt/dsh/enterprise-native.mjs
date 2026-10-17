// MEGMEET additions occupy native DSH slots. Local conversations, files and
// workspace navigation retain DSH ownership; plugin execution is explicit.
// Serialized into the native brand slot; no credential is stored outside the
// current password input and the single authenticated management request.
// Personal authorization stays at the authenticated gateway. No CLI credential
// or authorization token is accepted, saved, or placed in a conversation here.
export function createDingTalkCliPanel(React, { button, buttonStyle }) {
  const { createElement: h, useState, useEffect, useRef } = React;
  const prefix = '/__megmeet/enterprise/connectors/dingtalk/cli/';
  const labels = { authorization_required: '待连接', authorizing: '等待授权', ready: '已连接', unavailable: '暂不可用' };
  const safeAuthorization = value => {
    try {
      const url = new URL(value);
      const official = (url.hostname === 'login.dingtalk.com' && !url.hash)
        || (url.hostname === 'open-dev.dingtalk.com' && url.pathname === '/fe/old' && /^#\/personalAuthorization\?/.test(url.hash));
      return url.protocol === 'https:' && official && !url.port && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  };
  const display = value => typeof value === 'string' ? value.replace(/[\x00-\x1f]/g, '').slice(0, 300) : '';
  return function DingTalkCliPanel({ onChanged }) {
    const [state, setState] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const alive = useRef(true), request = useRef(null), changed = useRef(onChanged), lastStatus = useRef(null);
    changed.current = onChanged;
    async function refresh(action = 'status') {
      if (request.current) return;
      const controller = new AbortController(); request.current = controller;
      setError(''); if (action !== 'status') setBusy(true);
      try {
        const response = await fetch(prefix + action, { method: action === 'status' ? 'GET' : 'POST',
          credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: AbortSignal.any([controller.signal, AbortSignal.timeout(15000)]),
          ...(action === 'status' ? {} : { headers: { 'Content-Type': 'application/json' }, body: '{}' }) });
        if (!response.ok) throw Error();
        const data = await response.json();
        if (action === 'disconnect') {
          if (data.status !== 'disconnected') throw Error();
          if (alive.current) { lastStatus.current='authorization_required';setState({ status: 'authorization_required' }); await changed.current?.(); }
        } else {
          if (data.provider !== 'dingtalk_workspace_cli' || !Object.hasOwn(labels, data.status)) throw Error();
          if (alive.current) {
            const becameReady=lastStatus.current!==data.status&&data.status==='ready';lastStatus.current=data.status;
            setState({ status: data.status, status_message: display(data.status_message),
                authorization_url: safeAuthorization(data.authorization_url),
                expires_at: Number.isFinite(data.expires_at) ? data.expires_at : null,
                capabilities: Array.isArray(data.capabilities) ? data.capabilities.slice(0, 30).map(item => display(item.label)).filter(Boolean) : [] });
            if(becameReady)await changed.current?.();
          }
        }
      } catch (e) { if (alive.current && !controller.signal.aborted) setError('连接状态暂时无法更新，请稍后重试。'); }
      finally { if (request.current === controller) request.current = null; if (alive.current) setBusy(false); }
    }
    useEffect(() => { alive.current = true; void refresh(); return () => { alive.current = false; request.current?.abort(); }; }, []);
    useEffect(() => { if (state?.status !== 'authorizing') return; const timer = setInterval(() => { void refresh(); }, 2000); return () => clearInterval(timer); }, [state?.status]);
    return h('section', { 'data-megmeet-dingtalk-cli': '', 'aria-label': '我的钉钉连接' },
      h('strong', null, '钉钉'), h('p', { role: 'status', style: { fontSize: 12, lineHeight: 1.6 } },
        state ? labels[state.status] + ' · ' + (state.status_message || '通过钉钉官方授权连接本人账号。') : '正在检查连接…'),
      error ? h('p', { role: 'alert' }, error) : null,
      h('div', { style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
        state?.status === 'ready' ? button('断开我的连接', { disabled: busy, style: buttonStyle, onClick: () => void refresh('disconnect') }) :
          button(state?.status === 'authorizing' ? '重新检查授权' : '连接钉钉', { disabled: busy, style: buttonStyle, onClick: () => void refresh(state?.status === 'authorizing' ? 'status' : 'login') }),
        button('刷新状态', { disabled: busy, style: buttonStyle, onClick: () => void refresh() })),
      state?.status === 'authorizing' && state.authorization_url ? h('a', { href: state.authorization_url, target: '_blank', rel: 'noopener noreferrer',
        'data-megmeet-dingtalk-authorize': '', style: { display: 'inline-block', paddingTop: 12 },
        onClick: event => { if (state.expires_at && Date.now() >= (state.expires_at < 1e12 ? state.expires_at * 1000 : state.expires_at)) { event.preventDefault(); setError('授权入口已过期，请重新连接。'); } } }, '打开钉钉完成授权') : null,
      state?.status === 'ready' && state.capabilities?.length ? h('p', { style: { fontSize: 12 } }, '可用功能：' + state.capabilities.join('、')) : null,
      h('p', { style: { fontSize: 11, opacity: .7, lineHeight: 1.6 } }, '仅连接本人授权的钉钉。发送消息、创建或修改内容时，会先在当前对话请你确认。'));
  };
}

export function createRequestPlan(selection, input, files, requestId, sessionId, location, conversationId) {
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const nativeId = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const plugin = selection?.plugin;
  if (!plugin || !/^[a-z0-9][a-z0-9._-]{0,99}$/i.test(plugin.plugin_key) || !plugin.version) throw Error('请选择已授权的企业技能');
  if (!uuid.test(requestId) || !nativeId.test(sessionId) || !['local', 'cloud'].includes(location)) throw Error('当前会话信息无效');
  if (conversationId && !uuid.test(conversationId)) throw Error('插件会话信息无效');
  if (!input.trim() || input.length > 20000) throw Error('请输入消息，最多 20000 字');
  if (files.length > 16 || files.some(f => f.size > 262144)) throw Error('最多 16 个 UTF-8 文本文件，每个不超过 256 KiB');
  const body = { request_id: requestId, plugin_key: plugin.plugin_key, plugin_version: plugin.version,
    native_session_id: sessionId, location, input, attachment_ids: [], ...(conversationId ? { conversation_id: conversationId } : {}) };
  const scope = { request_id: requestId, plugin_key: plugin.plugin_key, plugin_version: plugin.version, explicit_upload: true };
  return { endpoint: '/plugins/tasks', body, scope };
}

// Values come only from the authenticated catalog/handoff proxy. This validator
// contains no token storage and returns no identity supplied by the renderer.
export function validateDingTalkAuthorization(value, desktop, origin, now = Date.now()) {
  const raw = desktop ? value?.authorization_url : value;
  if (typeof raw !== 'string' || !raw || raw.length > 4096) throw Error('INVALID_AUTHORIZATION');
  const url = new URL(raw, origin);
  if (url.username || url.password) throw Error('INVALID_AUTHORIZATION');
  if (desktop) {
    if (url.protocol !== 'https:' || url.pathname !== '/v1/connectors/dingtalk/desktop-handoff' || url.search || !/^#[A-Za-z0-9_-]{43}$/.test(url.hash) || value.handoff !== true || !Number.isFinite(value.expires_in) || value.expires_in <= 0 || value.expires_in > 60) throw Error('INVALID_HANDOFF');
    return { url: url.href, expiresAt: now + value.expires_in * 1000 };
  }
  if (url.origin !== origin || url.pathname !== '/v1/connectors/dingtalk/connect' || url.hash || url.protocol !== 'https:') throw Error('INVALID_AUTHORIZATION');
  return { url: url.href, expiresAt: now + 60000 };
}

// Fixed same-origin human controls. No callback accepts a URL, worker route,
// account identifier or credential; the authenticated proxy supplies identity.
export function createProjectActions(request, download, uuid = () => crypto.randomUUID()) {
  const sid = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const keys = new Set(['meeting','employee-cards','infrastructure-monitor','heatcost']);
  const context = value => { if (!sid.test(value.sessionId || '') || !keys.has(value.connector)) throw Error('PROJECT_CONTEXT_REQUIRED'); return value.sessionId; };
  return {
    async invoke(value) { const session_id=context(value); if(typeof value.action!=='string'||!value.action||value.action.length>100)throw Error('PROJECT_ACTION_INVALID'); return request('/__megmeet/tools/invoke',{session_id,connector:value.connector,action:value.action,arguments:value.arguments||{},request_id:uuid()}); },
    async download(value) { const session_id=context(value); if(!/^pjf_[A-Za-z0-9_-]{32}$/.test(value.attachmentId||''))throw Error('PROJECT_ATTACHMENT_INVALID'); return download('/__megmeet/projects/artifact?'+new URLSearchParams({session_id,attachment_id:value.attachmentId,disposition:'attachment'}),String(value.name||'下载文件').replace(/[\\/\r\n\0]/g,'_').slice(0,200)); },
    preview(value) { const session_id=context(value); if(value.connector!=='employee-cards'||!/^pjf_[A-Za-z0-9_-]{32}$/.test(value.attachmentId||''))throw Error('PROJECT_ATTACHMENT_INVALID'); return '/__megmeet/projects/artifact?'+new URLSearchParams({session_id,attachment_id:value.attachmentId,disposition:'inline'}); },
    async secureLogin(value) {
      const session_id=context(value);if(value.connector!=='infrastructure-monitor')throw Error('PROJECT_FORM_INVALID');
      const fields={location:String(value.values?.location||''),username:String(value.values?.username||''),password:String(value.values?.password||'')};
      try {
        if(!fields.location||fields.location.length>120||!fields.username||fields.username.length>120||!fields.password||fields.password.length>512)throw Error('PROJECT_FORM_INVALID');
        const ticket=await request('/__megmeet/tools/form-ticket',{session_id,form:'monitor-login'});
        if(!/^[A-Za-z0-9_-]{43}$/.test(ticket.ticket||'')||ticket.form!=='monitor-login')throw Error('PROJECT_TICKET_INVALID');
        return await request('/__megmeet/tools/secure-form',{session_id,ticket:ticket.ticket,fields,request_id:uuid()});
      } finally { fields.password='';if(value.values)value.values.password=''; }
    },
    async cancel({sessionId,jobId}) {if(!sid.test(sessionId||'')||!/^[A-Za-z0-9_-]{1,128}$/.test(jobId||''))throw Error('PROJECT_JOB_INVALID');return request('/__megmeet/tools/cancel-job',{session_id:sessionId,job_id:jobId});}
  };
}

// Serialized by the version-guarded brand adapter; keep this function closed
// over its parameters and browser globals, not module-scope helpers.
export function installEnterprise(ctx, require, makePlan, makeProjectRenderer, projectHandlers = {}, validateAuthorization, makeProjectActions) {
  const React = require('react');
  const { createPortal } = require('react-dom');
  const { createElement: h, useEffect, useLayoutEffect, useRef, useState } = React;
  const selections = new Map(), results = new Map(), listeners = new Set(), inflight = new Set();
  const toolStates=new Map();
  // The worker routes only these four projects at native message admission.
  // Choosing a skill remains a session-scoped selection, never an execution.
  const workflowNames = { meeting: '会议文档', 'employee-cards': '工卡制作', heatcost: '图纸成本分析', 'infrastructure-monitor': 'vmware' };
  const isWorkflow = key => Object.hasOwn(workflowNames, key);
  const skillName = plugin => workflowNames[plugin.plugin_key] || plugin.name;
  let commandContext, workspaceContext;
  const announce = () => listeners.forEach(fn => fn());
  const errors = { 401: '登录已过期，请重新登录。', 403: '你尚未获得此插件的使用权限。', 409: '会话或任务状态已变化，草稿已保留，请重试。', 503: '服务暂时不可用，请保留消息后重试。' };
  const confirmationMessages={dingtalk_cli_write_result_unknown:'操作结果尚未确认，请先到钉钉核对，避免重复操作。',connector_write_result_unknown_check_dingtalk:'操作结果尚未确认，请先到钉钉核对，避免重复操作。',dingtalk_cli_commands_changed:'钉钉工具或授权已变化，本次未执行，请刷新连接后重新发起。',connector_confirmation_consumed_or_expired:'此确认已处理或过期，请先核对钉钉中的结果。'};
  async function request(url, body, confirmation=false) {
    const response = await fetch(url, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(120000) });
    if (!response.ok) {
      let code;if(confirmation){try{const value=await response.json();if(Object.hasOwn(confirmationMessages,value?.detail))code=value.detail;}catch{}}
      const error=Error(code?confirmationMessages[code]:errors[response.status] || `请求未完成 (${response.status})`);if(code)error.code=code;throw error;
    }
    return response.json();
  }
  if (typeof makeProjectActions === 'function') projectHandlers = { ...makeProjectActions(request, async (href, name) => { const link=document.createElement('a');link.href=href;link.download=name;document.body.append(link);try{link.click();}finally{link.remove();} }), ...projectHandlers };
  const api = (path, body) => request('/__megmeet/enterprise' + path, body);
  const mode = () => request('/__megmeet/desktop/workspace');
  const cloudIntentKey = 'megmeet.explicit-cloud-open.v1';
  const buttonStyle = { border: '1px solid var(--dsw-alias-border-l2, #e1e5df)', borderRadius: 10, padding: '6px 10px', background: 'transparent', color: 'inherit', cursor: 'pointer', font: 'inherit' };
  const button = (label, props = {}) => h('button', { type: 'button', className: 'mm-soft-button', style: buttonStyle, ...props }, label);
  const DingTalkCliPanel=createDingTalkCliPanel(React,{button,buttonStyle});
  const icon = (kind, size = 16) => h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true },
    ...(kind === 'spark' ? ['M12 3l2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3'] : kind === 'check' ? ['M5 12l4 4L19 6'] : kind === 'arrow' ? ['M9 5l7 7-7 7'] : kind === 'search' ? ['M21 21l-5-5','M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0'] : ['M6 6l12 12','M18 6L6 18']).map((d, index) => h('path', { key: index, d })));
  const theme = `.mm-sidebar-wordmark-container{display:flex;align-items:center;flex:none;width:144px;height:24px;overflow:hidden}.mm-sidebar-wordmark{width:144px;height:auto;max-width:none;display:block}[data-sidebar-collapsed=true] .mm-sidebar-wordmark-container{width:32px;height:24px}[data-sidebar-collapsed=true] .mm-sidebar-wordmark{width:190px;height:auto;flex:none}.mm-soft-button{box-sizing:border-box;transition:background .15s,border-color .15s;line-height:1.4}.mm-soft-button:hover:not(:disabled){background:var(--dsw-alias-interactive-bg-hover,#f1f4ef)}.mm-soft-button:focus-visible,.mm-skill-row:focus-visible,.mm-skill-search:focus-visible{outline:2px solid #40af9f;outline-offset:3px}.mm-soft-button:disabled{opacity:.5;cursor:default}.mm-skill-popover{font-family:inherit;font-size:13px;line-height:1.5;background:var(--dsw-alias-bg-base,#fffefb);color:var(--dsw-alias-label-primary,#273c38);border:1px solid var(--dsw-alias-border-l2,#e3e8e0);border-radius:18px;box-shadow:0 18px 58px #223b3020,0 2px 8px #223b3010}.mm-skill-search{box-sizing:border-box;width:100%;border:0;border-radius:11px;background:var(--dsw-alias-interactive-bg-hover,#f3f5ef);color:inherit;padding:9px 12px 9px 34px;font:inherit}.mm-skill-row{display:flex;gap:11px;align-items:flex-start;text-align:left;width:100%;border:0;border-radius:12px;background:transparent;color:inherit;padding:11px;cursor:pointer;font:inherit;transition:background .15s}.mm-skill-row:hover{background:var(--dsw-alias-interactive-bg-hover,#f1f5ef)}.mm-skill-row[aria-pressed=true]{background:color-mix(in srgb,#40af9f 12%,transparent)}.mm-skill-icon{display:grid;place-items:center;flex:none;width:32px;height:32px;border-radius:10px;background:color-mix(in srgb,#40af9f 11%,transparent);color:#339c8d}.mm-skill-muted{color:var(--dsw-alias-label-tertiary,#70817a);font-size:12px;line-height:1.55}.mm-skill-badge{font-size:10px;white-space:nowrap;padding:2px 6px;border-radius:6px;background:var(--dsw-alias-interactive-bg-hover,#f1f4ef);color:var(--dsw-alias-label-tertiary,#70817a)}.mm-skill-detail{background:var(--dsw-alias-interactive-bg-hover,#f4f6f0);border-radius:12px;margin:4px 0 8px;padding:12px}.mm-skill-check{display:flex;gap:7px;align-items:center;margin:5px 0}.mm-skill-check[data-ready=true]{color:#318575}.mm-skill-chip{display:inline-flex;align-items:center;gap:5px;border-radius:99px;background:color-mix(in srgb,#40af9f 12%,transparent);color:var(--dsw-alias-label-primary,#315c52);padding:3px 4px 3px 10px;font-size:11px}.mm-skill-chip button{display:grid;place-items:center;border:0;background:transparent;color:inherit;border-radius:50%;width:22px;height:22px;cursor:pointer}.mm-skill-launcher{max-width:100%;position:relative;display:flex;flex-wrap:wrap;gap:5px;align-items:center}.mm-skill-launcher>button:first-of-type{display:inline-flex;align-items:center;gap:6px;border-color:transparent;color:var(--dsw-alias-label-primary-dimmed,#63746c);border-radius:99px;padding:6px 9px}.mm-skill-launcher>button[aria-expanded=true]{background:color-mix(in srgb,#40af9f 12%,transparent);color:#2a887b}@media(max-width:700px){.mm-skill-popover{border-radius:16px}}@media(prefers-reduced-motion:reduce){.mm-soft-button,.mm-skill-row{transition:none}}`;
  if (ctx.effect) ctx.effect(() => {
    const style = document.createElement('style'); style.dataset.megmeetSkillsTheme = 'v1'; style.textContent = theme; document.head.append(style);
    return () => style.remove();
  }, 'megmeet: skills theme');
  // Portal-owned panels cannot be clipped by the collapsed native sidebar.
  // Clamp against the actual viewport, including short windows and long details.
  function useFloating(anchor, content, enabled, width, mobile = false) {
    const [position, setPosition] = useState({ position: 'fixed', visibility: 'hidden', left: 12, top: 12, width });
    useLayoutEffect(() => {
      if (!enabled || !anchor.current || !content.current) return;
      const update = () => {
        const viewWidth = window.innerWidth, viewHeight = window.innerHeight;
        const isMobile = mobile && viewWidth <= 700;
        const actualWidth = Math.min(isMobile ? viewWidth - 24 : width, viewWidth - 24);
        const maxHeight = Math.min(560, viewHeight - 24);
        const trigger = anchor.current.getBoundingClientRect();
        const height = Math.min(content.current.getBoundingClientRect().height, maxHeight);
        const left = isMobile ? 12 : Math.max(12, Math.min(trigger.left, viewWidth - actualWidth - 12));
        const top = isMobile ? Math.max(12, viewHeight - height - 12) : Math.max(12, Math.min(trigger.top - height - 10, viewHeight - height - 12));
        const next = { position: 'fixed', visibility: 'visible', left, top, width: actualWidth, maxHeight, boxSizing: 'border-box' };
        setPosition(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
      };
      update();
      const observer = new ResizeObserver(update); observer.observe(content.current); observer.observe(anchor.current);
      window.addEventListener('resize', update); window.addEventListener('scroll', update, true);
      return () => { observer.disconnect(); window.removeEventListener('resize', update); window.removeEventListener('scroll', update, true); };
    }, [enabled, width, mobile]);
    return position;
  }
  function observe() {
    const [, redraw] = useState(0);
    useEffect(() => { const fn = () => redraw(x => x + 1); listeners.add(fn); return () => { listeners.delete(fn); }; }, []);
  }
  const getFacade = id => {
    const actx = commandContext?.get('sessions')?.scope(id);
    return actx?.get('conversation')?.input.for(actx);
  };
  function PinnedWorkspace({ wide = true }) {
    const [state, setState] = useState({ mode: 'local' }), [busy, setBusy] = useState(false), [error, setError] = useState('');
    const alive = useRef(true), switching = useRef(false);
    const [queued, setQueued] = useState(0);
    useEffect(() => {
      alive.current = true;
      let disposeIntent = () => {};
      // Carry only a successful manual click across the Host-switch reload.
      // Consume it immediately: a normal launch/refresh/archive cannot recreate
      // this intent or automatically choose a cloud workspace later.
      let intent = 0;
      try {
        intent = Number(window.sessionStorage?.getItem(cloudIntentKey));
        window.sessionStorage?.removeItem(cloudIntentKey);
      } catch {}
      mode().then(value => {
        if (!alive.current) return;
        setState(value);
        if(value.client==='web'&&!intent) intent=Date.now();
        if (value.mode !== 'cloud' || !intent || Date.now() < intent || Date.now() - intent > 120000) return;
        const store = workspaceContext?.get('workspaces')?.list;
        const connection = workspaceContext?.get('connection')?.state;
        const navigation = workspaceContext?.get('uiWorkspace');
        if (!store || !connection || !navigation) return;
        let done = false, timer;
        const disposers = [];
        const stop = () => { done = true; clearTimeout(timer); disposers.splice(0).forEach(fn => fn()); };
        const select = () => {
          if (done || !alive.current) return;
          const snapshot = store.getSnapshot();
          if (connection.getSnapshot() !== 'connected' || snapshot.phase !== 'ready') return;
          if (!snapshot.items.length) return;
          stop();
          if (snapshot.items.length !== 1) { setError('云端工作区状态异常，请稍后重试。'); return; }
          const sessions = commandContext?.get('sessions')?.list?.getSnapshot();
          const drafts = Object.keys(sessions?.byId || {}).map(id=>getFacade(id)?.state?.getSnapshot());
          if (Object.values(sessions?.byId || {}).some(s => s.running) || drafts.some(d=>d?.draft?.trim()||d?.attachmentIds?.length)) {
            setError('云端已有任务或草稿，请处理后点击云端工作区。'); return;
          }
          Promise.resolve(navigation.openWorkspace(snapshot.items[0].workspaceId)).catch(e => { if (alive.current) setError(e.message); });
        };
        if (store.subscribe) disposers.push(store.subscribe(select));
        if (connection.subscribe) disposers.push(connection.subscribe(select));
        disposeIntent = stop;
        select();
        if (!done) timer = setTimeout(() => { stop(); if (alive.current) setError('云端尚未就绪，请点击云端工作区重试。'); }, 30000);
      }).catch(() => {});
      return () => { alive.current = false; disposeIntent(); };
    }, []);
    const open = async target => {
      if (switching.current) return;
      const sessions = commandContext?.get('sessions')?.list?.getSnapshot();
      if (inflight.size || Object.values(sessions?.byId || {}).some(s => s.running)) {
        setError('请等当前任务完成后再切换工作区。'); return;
      }
      const drafts = Object.keys(sessions?.byId || {}).map(id=>getFacade(id)?.state?.getSnapshot());
      if (drafts.some(d=>d?.draft?.trim()||d?.attachmentIds?.length)) {
        setError('请先发送或清空当前草稿和附件，再切换工作区。'); return;
      }
      switching.current = true; setBusy(true); setQueued(0); setError('');
      try {
        if (target === 'cloud' && state.mode === 'cloud') {
          const snapshot = workspaceContext?.get('workspaces')?.list?.getSnapshot();
          const navigation = workspaceContext?.get('uiWorkspace');
          const connection = workspaceContext?.get('connection')?.state?.getSnapshot();
          if (snapshot?.items?.length > 1) throw Error('云端工作区状态异常，请稍后重试。');
          // This list comes from the authenticated cloud Host, never a browser-
          // supplied path. Even after archiving every chat this is explicit.
          if (connection === 'connected' && snapshot?.phase === 'ready' && snapshot.items.length === 1 && navigation) {
            await navigation.openWorkspace(snapshot.items[0].workspaceId);
            switching.current = false; if (alive.current) setBusy(false);
            return;
          }
          // The explicit click also recovers an expired/idle cloud worker.
          // No timer or initial render is allowed to start a remote Host.
        }
        let result;
        const deadline = Date.now() + 120000;
        do {
          result = await request(`/__megmeet/desktop/${target}/open`, {});
          if (!['queued','starting'].includes(result.status)) break;
          if (!alive.current) return;
          if (Date.now() >= deadline) throw Error('云端仍繁忙，已结束本次排队。可以继续本机聊天，稍后再试。');
          setQueued(result.phase==='starting'||result.status==='starting'?'starting':result.position);
          await new Promise(resolve => setTimeout(resolve, 5000));
          if (!alive.current) return;
        } while (true);
        if (result.mode !== target) throw Error('工作区未能切换，请重试。');
        if (alive.current) {
          try {
            if (target === 'cloud') window.sessionStorage?.setItem(cloudIntentKey, String(Date.now()));
            else window.sessionStorage?.removeItem(cloudIntentKey);
          } catch {}
          window.location.reload();
        }
      } catch (e) { if (alive.current) setError(e.message); }
      finally { switching.current = false; if (alive.current) { setBusy(false); setQueued(0); } }
    };
    return h('div', { 'data-megmeet-pinned-workspace': '', style: { padding: wide ? '3px 10px 8px' : '4px', display: 'grid', gap: 5 } },
      button(wide ? (busy ? '正在连接…' : '云端工作区') : '☁', { onClick: () => open('cloud'), disabled: busy,
        'aria-label': '云端工作区', 'aria-current': state.mode === 'cloud' ? 'location' : undefined,
        title: '个人云端工作区 · 30 天未访问自动清空 · 归档永久删除', 'data-megmeet-cloud-open': '',
        style: { ...buttonStyle, textAlign: wide ? 'left' : 'center', width: '100%', borderColor: state.mode === 'cloud' ? '#3b82f6' : '#8883' } }),
      state.mode === 'cloud' && state.client !== 'web' ? button(wide ? '返回本机工作区' : '⌂', { onClick: () => open('local'), disabled: busy, 'aria-label': '返回本机工作区', 'data-megmeet-local-open': '' }) : null,
      queued ? h('small', {role:'status'}, queued==='starting'?'正在准备云端工作区…':`云端繁忙，正在排队（第 ${queued} 位）`) : null,
      error ? h('small', { role: 'alert' }, error) : null);
  }
  function PluginPopover({ onChoose, onClose, selected = [], anchor, sessionId }) {
    const [client, setClient] = useState(null), [authLink, setAuthLink] = useState(null), [authorizing, setAuthorizing] = useState(false);
    const [plugins, setPlugins] = useState(null), [error, setError] = useState(''), [query, setQuery] = useState(''), [detail, setDetail] = useState(null);
    const root = useRef(null), search = useRef(null), refreshCatalog=useRef(async()=>{});
    const floating = useFloating(anchor, root, true, 384, true);
    useEffect(() => { if (floating.visibility === 'visible') search.current?.focus(); }, [floating.visibility]);
    useEffect(() => {
      let live = true, pending = false;
      const refresh = async () => {
        if (pending) return; pending = true;
        try {
          const values = await Promise.allSettled([api('/plugins'), request('/__megmeet/tools/catalog?session_id='+encodeURIComponent(sessionId))]);
          if (!live) return;
          const legacy = values[0].status === 'fulfilled' ? (values[0].value.items || []) : [];
          const tools = values[1].status === 'fulfilled' ? (values[1].value.items || []).map(p => ({ ...p, connector: true })) : [];
          const next = [...tools, ...legacy];
          setPlugins(next); setDetail(current => current ? (next.find(p => p.plugin_key === current.plugin_key) || current) : null);
          setError(values.every(v => v.status === 'rejected') ? '技能暂时无法加载，请稍后重试。' : values.some(v => v.status === 'rejected') ? '部分技能暂时未加载，已显示可用内容。' : '');
        } finally { pending = false; }
      };
      const previousFocus = document.activeElement;
      refreshCatalog.current=refresh;
      void refresh();
      mode().then(value => { if (live) setClient(value.client === 'web' ? 'web' : 'desktop'); }).catch(() => { if (live) setError('无法确认当前客户端，请关闭后重试。'); });
      const keydown = e => {
        if (e.key === 'Escape') { e.stopPropagation(); onClose(); return; }
        if (e.key !== 'Tab' || !root.current) return;
        const focusable = [...root.current.querySelectorAll('button:not(:disabled),input:not(:disabled),a[href],summary')];
        const first = focusable[0], last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last?.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus(); }
      };
      const outside = e => { if (root.current && !root.current.contains(e.target) && !e.target.closest?.('[data-megmeet-plugin-picker]')) onClose(); };
      window.addEventListener('keydown', keydown); window.addEventListener('pointerdown', outside); window.addEventListener('focus', refresh);
      return () => { live = false; refreshCatalog.current=async()=>{};window.removeEventListener('keydown', keydown); window.removeEventListener('pointerdown', outside); window.removeEventListener('focus', refresh); previousFocus?.focus?.(); };
    }, []);
    useEffect(() => {
      if (!authLink) return;
      const timer = setTimeout(() => { setAuthLink(null); setError('授权入口已过期，请重新点击连接。'); }, Math.max(0, authLink.expiresAt - Date.now()));
      return () => clearTimeout(timer);
    }, [authLink]);
    const available = plugin => !plugin.connector || ['ready', 'available', 'limited'].includes(plugin.status);
    const enterpriseSettings = plugin => plugin.plugin_key==='dingtalk'&&plugin.enterprise_oauth&&typeof plugin.enterprise_oauth==='object'?{...plugin,...plugin.enterprise_oauth}:plugin;
    const filtered = (plugins || []).filter(plugin => `${skillName(plugin)} ${plugin.description || ''}`.toLowerCase().includes(query.trim().toLowerCase()));
    const authorize = async plugin => {
      if (!client || authorizing) return;
      setAuthorizing(true); setAuthLink(null); setError('');
      try {
        const desktop = client !== 'web';
        const value = desktop ? await api('/connectors/dingtalk/desktop-handoff', {}) : enterpriseSettings(plugin).authorization_url;
        const link = validateAuthorization(value, desktop, location.origin);
        setAuthLink(link);
        const popup = window.open(link.url, '_blank', 'noopener,noreferrer');
        if (popup) popup.opener = null;
        setError(desktop ? '请在系统浏览器完成钉钉连接。若未打开，请点击下方入口。' : '请在新窗口完成钉钉连接。若未打开，请点击下方入口。');
      } catch { setError('钉钉授权暂时无法启动，请重试或联系管理员。'); }
      finally { setAuthorizing(false); }
    };
    return createPortal(h('section', { ref: root, role: 'dialog', 'aria-label': '选择技能', 'data-megmeet-plugin-popover': '', className: 'mm-skill-popover',
      style: { ...floating, display: 'flex', flexDirection: 'column', overflow: 'hidden', zIndex: 80, padding: 12 } },
      h('header', { style: { display: 'flex', flexShrink: 0, alignItems: 'center', justifyContent: 'space-between', gap: 10, padding: '2px 2px 10px' } },
        h('div', null, h('strong', { style: { fontSize: 14, fontWeight: 600 } }, '技能'), h('div', { className: 'mm-skill-muted', style: { paddingTop: 3 } }, '选择需要的技能，随消息发送')),
        button(icon('close'), { onClick: onClose, 'aria-label': '关闭技能选择', style: { ...buttonStyle, display: 'grid', placeItems: 'center', border: 0, padding: 6 } })),
      h('div', { style: { position: 'relative', flexShrink: 0, marginBottom: 8 } },
        h('span', { style: { position: 'absolute', top: 10, left: 11, color: 'var(--dsw-alias-label-tertiary,#70817a)' } }, icon('search')),
        h('input', { ref: search, className: 'mm-skill-search', value: query, onChange: e => setQuery(e.target.value), placeholder: '搜索技能', 'aria-label': '搜索技能' })),
      h('div', { 'data-megmeet-skill-scroll': '', style: { overflowY: 'auto', minHeight: 0, overscrollBehavior: 'contain' } },
      error ? h('p', { role: 'status', className: 'mm-skill-muted', style: { margin: '8px 4px' } }, error) : null,
      authLink ? h('a', { href: authLink.url, target: '_blank', rel: 'noopener noreferrer', 'data-megmeet-dingtalk-handoff': '', onClick: e => { if (Date.now() >= authLink.expiresAt) { e.preventDefault(); setAuthLink(null); setError('授权入口已过期，请重新点击连接。'); } }, style: { display: 'inline-block', padding: '6px 4px', color: 'var(--dsw-alias-label-link,#268c7e)' } }, '打开钉钉连接页') : null,
      plugins === null ? h('p', { role: 'status', className: 'mm-skill-muted', style: { padding: 12 } }, '正在加载技能…') : !filtered.length ? h('p', { className: 'mm-skill-muted', style: { padding: 12 } }, query ? '没有匹配的技能' : '暂无已授权的技能') :
        filtered.map(plugin => h('div', { key: `${plugin.plugin_key}@${plugin.version || ''}` },
          h('button', { type: 'button', className: 'mm-skill-row', 'data-megmeet-plugin-key': plugin.plugin_key, 'aria-pressed': selected.includes(plugin.plugin_key),
            onClick: () => available(plugin) ? onChoose({ plugin: { ...plugin } }) : setDetail(detail?.plugin_key === plugin.plugin_key ? null : plugin) },
            h('span', { className: 'mm-skill-icon' }, icon(selected.includes(plugin.plugin_key) ? 'check' : 'spark')),
            h('span', { style: { flex: 1, minWidth: 0 } }, h('span', { style: { display: 'flex', gap: 7, alignItems: 'center' } },
              h('strong', { style: { fontWeight: 500 } }, skillName(plugin)), !available(plugin) ? h('span', { className: 'mm-skill-badge' }, plugin.status === 'authorization_required' ? '待连接' : '待配置') : isWorkflow(plugin.plugin_key) ? h('span', { className: 'mm-skill-badge', 'data-megmeet-workflow-badge': plugin.plugin_key }, selected.includes(plugin.plugin_key) ? '已选 · 发送后执行' : '发送后执行') : selected.includes(plugin.plugin_key) ? h('span', { className: 'mm-skill-badge' }, '已选') : null),
              plugin.description ? h('span', { className: 'mm-skill-muted', style: { display: 'block', marginTop: 3 } }, plugin.description) : null,
              available(plugin) && plugin.connector ? h('span', { className: 'mm-skill-muted', style: { display: 'block', marginTop: 3 }, 'data-megmeet-skill-behavior': isWorkflow(plugin.plugin_key) ? 'workflow' : 'model' }, isWorkflow(plugin.plugin_key) ? '发送后运行项目流程，缺少的信息会在对话中提示。' : '由模型根据当前问题按需使用。') : null),
            h('span', { style: { marginTop: 5, color: 'var(--dsw-alias-label-tertiary,#70817a)' } }, selected.includes(plugin.plugin_key) ? icon('check', 14) : available(plugin) ? null : icon('arrow', 14))),
          plugin.plugin_key==='dingtalk'||plugin.capabilities?.length && available(plugin) ? button(plugin.plugin_key==='dingtalk'?'管理我的钉钉连接':'查看功能与连接状态', { onClick: () => setDetail(detail?.plugin_key === plugin.plugin_key ? null : plugin), 'data-megmeet-dingtalk-manage':plugin.plugin_key==='dingtalk'?'':undefined,style: { ...buttonStyle, border: 0, fontSize: 11, marginLeft: 43, marginTop: -5, padding: '2px 4px 6px' } }) : null,
          detail?.plugin_key === plugin.plugin_key ? h('div', { className: 'mm-skill-detail', 'data-megmeet-skill-detail': plugin.plugin_key },
            plugin.plugin_key==='dingtalk'?h(DingTalkCliPanel,{onChanged:()=>refreshCatalog.current()}):null,
            plugin.plugin_key!=='dingtalk'?h('div',null,

            h('p', { style: { margin: '0 0 8px', fontSize: 12 } }, enterpriseSettings(plugin).status_message || (enterpriseSettings(plugin).status === 'authorization_required' ? '连接本人的钉钉后即可使用。' : available(enterpriseSettings(plugin)) ? '此技能已准备就绪。' : '此技能尚未配置完成，请联系平台管理员。')),
            (enterpriseSettings(plugin).setup?.checks || []).map(check => h('div', { key: check.key, className: 'mm-skill-check', 'data-ready': check.ready }, icon(check.ready ? 'check' : 'close', 13), h('span', { style: { fontSize: 11 } }, check.label))),
            (plugin.capabilities || []).map(capability => h('div', { key: capability.key, className: 'mm-skill-muted', style: { marginTop: 6 } }, capability.label, ' · ', capability.available ? '可用' : '待开通', capability.requires_confirmation ? ' · 执行前确认' : '', capability.note ? h('div', null, capability.note) : null)),
            (enterpriseSettings(plugin).status === 'authorization_required'||enterpriseSettings(plugin).setup?.state==='authorization_required') ? button(authorizing ? '正在打开…' : '连接企业钉钉账户', { disabled: authorizing || !client, 'data-megmeet-dingtalk-connect': '', onClick: () => authorize(plugin), style: { ...buttonStyle, marginTop: 12, background: '#329d8c', color: '#fff', borderColor: 'transparent' } }) : null):null) : null))),
      h('footer', { className: 'mm-skill-muted', style: { flexShrink: 0, fontSize: 11, borderTop: '1px solid var(--dsw-alias-border-l2,#e6eae3)', marginTop: 7, padding: '9px 3px 1px' } }, '仅在当前对话启用，可随时取消选择。项目技能发送后执行，其他技能由模型按需使用。')), document.body);
  }

  async function readToolState(id){const value=await request('/__megmeet/tools/state?session_id='+encodeURIComponent(id));toolStates.set(id,value);announce();return value;}
  async function selectTools(id,plugins){const value=await request('/__megmeet/tools/selection',{session_id:id,plugins});toolStates.set(id,{...toolStates.get(id),...value,pending:[]});announce();}
  const dockStyle={boxSizing:'border-box',width:'calc(100% - var(--dsh-composer-side-clearance, 24px) - var(--dsh-composer-side-clearance, 24px))',maxWidth:'var(--dsh-composer-card-max-width, 780px)',margin:'0 auto',background:'var(--dsw-alias-bg-base, Canvas)',color:'var(--dsw-alias-label-primary, CanvasText)'};
  function previewFields(preview){
    const labels={text:'消息内容',title:'标题',description:'说明',start:'开始时间',end:'结束时间',due_time_ms:'截止时间',recipients:'收件人',attendees:'参与人',task_id:'待办标识',done:'完成状态',employees:'查询同事'};
    return Object.entries(preview?.arguments||{}).map(([key,value])=>{
      let content;
      if((key==='recipients'||key==='attendees'||key==='employees')&&Array.isArray(value)){
        content=value.map(no=>{const person=preview.recipients?.find(p=>p.employee_no===no);return person?`${person.name}（${no}）`:String(no);}).join('、')||'无';
      }else if(key==='done'&&typeof value==='boolean'){content=value?'已完成':'未完成';}else if(key==='due_time_ms'&&typeof value==='number')content=new Date(value).toLocaleString();
      else content=typeof value==='string'?value:JSON.stringify(value);
      return h('div',{key,style:{marginTop:6}},h('strong',null,(labels[key]||key)+'：'),h('span',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},content));
    });
  }
  function ToolPending({sessionId,useSession}){
    const session=useSession?.(s=>s),id=sessionId||session?.id||session?.sessionId;
    const [message,setMessage]=useState(''),[busy,setBusy]=useState(false),attempted=useRef(new Set());observe();
    const pending=toolStates.get(id)?.pending||[];
    useEffect(()=>{setMessage('');},[id]);
    const confirm=async(item,confirmed)=>{
      const key=id+':'+item.confirmation_id;if(attempted.current.has(key))return;attempted.current.add(key);
      setBusy(true);setMessage('');
      try{
        const value=await request('/__megmeet/tools/confirm',{session_id:id,confirmation_id:item.confirmation_id,confirmed},true);
        const failed=value.result?.isError===true;
        const message=!confirmed?'已取消操作。':failed?'钉钉未能完成此操作，请先核对钉钉中的状态，再决定下一步。':value.status==='completed'?'操作已完成。':'已收到操作结果，请核对下方状态。';
        const text=typeof value.result?.text==='string'?value.result.text:'';
        setMessage(message+(confirmed&&!failed?'\n'+(text||JSON.stringify(value.result?.structuredContent||value.result||value,null,2)).slice(0,4000):''));
        getFacade(id)?.notify('info',message);
      }catch(e){setMessage(confirmed?(confirmationMessages[e.code]||confirmationMessages.dingtalk_cli_write_result_unknown):'取消结果尚未确认，请刷新后核对状态。');}
      finally{try{await readToolState(id);}catch{}setBusy(false);}
    };
    if(!pending.length&&!message)return null;
    return h('div',{'data-megmeet-tool-confirmations':'',role:'region','aria-label':'操作确认',style:{...dockStyle,border:'1px solid #8885',borderRadius:10,padding:12,maxHeight:260,overflow:'auto'}},
      pending.map(item=>h('div',{key:item.confirmation_id},h('strong',null,item.plugin_name+' · 等待确认'),
        h('p',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},item.preview?.summary||'请核对以下操作'),
        item.preview?.provider==='dingtalk_workspace_cli'?h('p',{'data-megmeet-confirm-account':'',style:{fontSize:12,lineHeight:1.6}},'执行账号：'+([item.preview.account?.name,item.preview.account?.organization].filter(value=>typeof value==='string'&&value.trim()).map(value=>value.slice(0,120)).join(' · ')||'当前钉钉授权账号')):null,
        h('div',{style:{marginBottom:10,fontSize:13}},...previewFields(item.preview)),
        button(attempted.current.has(id+':'+item.confirmation_id)?'已提交，请核对结果':'确认执行',{onClick:()=>confirm(item,true),disabled:busy||attempted.current.has(id+':'+item.confirmation_id)}), ' ',button('取消',{onClick:()=>confirm(item,false),disabled:busy||attempted.current.has(id+':'+item.confirmation_id)}))),
      message?h('p',{role:'status'},message):null);
  }
  const projectUI = typeof makeProjectRenderer === 'function' ? makeProjectRenderer(React, projectHandlers) : null;
  function ProjectResults({sessionId,useSession}){
    const session=useSession?.(value=>value),id=sessionId||session?.id||session?.sessionId;observe();
    const [stopping,setStopping]=useState(null),[error,setError]=useState(''),[dismissed,setDismissed]=useState([]);
    const items=toolStates.get(id)?.results||[],jobs=(toolStates.get(id)?.jobs||[]).filter(job=>job&&['running','cancelling','failed','interrupted'].includes(job.status)&&/^[A-Za-z0-9_-]{1,128}$/.test(job.job_id||'')&&!dismissed.includes(job.job_id)).slice(0,16);
    useEffect(()=>{setStopping(null);setError('');setDismissed([]);},[id]);
    const cancel=async job=>{if(stopping)return;setStopping(job.job_id);setError('');try{await projectHandlers.cancel({sessionId:id,jobId:job.job_id});await readToolState(id);}catch{setError('任务正在结束，请稍后查看状态。');}finally{setStopping(null);}};
    if(!projectUI||(!items.length&&!jobs.length&&!error))return null;
    return h('section',{'aria-label':'项目技能结果','data-megmeet-project-results':'',style:{...dockStyle,maxHeight:440,overflow:'auto',paddingTop:8}},
      jobs.map(job=>{const failed=['failed','interrupted'].includes(job.status);return h('div',{key:job.job_id,'data-megmeet-project-job':job.job_id,style:{display:'flex',alignItems:'center',justifyContent:'space-between',gap:10,padding:'9px 13px',marginBottom:10,borderRadius:12,background:'var(--dsw-alias-interactive-bg-hover,#f1f5ef)',fontSize:12}},h('span',{role:failed?'alert':'status'},failed?(projectUI.failureText?.(job.detail)||'项目未完成，请稍后重试。'):job.status==='cancelling'||stopping===job.job_id?'正在结束任务…':'项目技能正在处理…'),failed?button('关闭',{onClick:()=>setDismissed(value=>[...value,job.job_id])}):button('停止任务',{disabled:!!stopping||job.status==='cancelling'||typeof projectHandlers.cancel!=='function',onClick:()=>cancel(job)}));}),
      error?h('p',{role:'status',className:'mm-skill-muted'},error):null,
      items.slice(-12).filter(projectUI.safeCard).map(card=>h(projectUI.ProjectCard,{key:id+':'+card.id,card,sessionId:id,onRefresh:()=>readToolState(id)})));
  }
  function EnterpriseDock(props){return h(React.Fragment,null,h(PluginResults,props),h(ProjectResults,props),h(ToolPending,props));}
  function Launcher({ useSession, useInput, sessionId }) {
    const trigger = useRef(null);
    const [open, setOpen] = useState(false),[error,setError]=useState(''),[toolBusy,setToolBusy]=useState(false);
    observe();
    const session = useSession?.(s => s), input = useInput?.(s => s);
    const id = sessionId || session?.id || session?.sessionId;
    useEffect(()=>{
      if(!id)return;let live=true;setError('');
      const refresh=()=>{if(live&&document.visibilityState!=='hidden')void readToolState(id).catch(()=>{});};
      refresh();const timer=setInterval(refresh,3000);window.addEventListener('focus',refresh);
      return()=>{live=false;clearInterval(timer);window.removeEventListener('focus',refresh);};
    },[id]);
    const toolIds=toolStates.get(id)?.selected||[];
    const choose = async selection => {
      const facade = getFacade(id);
      if (!facade || !input || !id || inflight.has(id)) return;
      if(selection.plugin.connector){
        setToolBusy(true);setError('');
        try{
          const next=toolIds.includes(selection.plugin.plugin_key)?toolIds.filter(key=>key!==selection.plugin.plugin_key):[...toolIds,selection.plugin.plugin_key];
          await selectTools(id,next);
          // Selection is bound server-side to this native session. Preserve the user's draft verbatim.
          setOpen(false);
        }catch(e){setError(e.message);}finally{setToolBusy(false);}
        return;
      }
      const existing = selections.get(id);
      const entry = { plugin: { ...selection.plugin }, token: '/企业任务 ', attempt: null };
      if (facade.beginCommand(claim(id, entry), { start: 0, end: existing && input.draft.startsWith(existing.token) ? existing.token.length : 0, draftRev: input.draftRev })) {
        selections.set(id, entry); announce(); setOpen(false);
      } else facade.notify('error', '当前输入正在处理，请稍后选择技能。');
    };
    const selected = id && input?.draft?.startsWith('/企业任务') && selections.get(id);
    const remove = () => {
      if (inflight.has(id) || input?.phase === 'submitting') return;
      const facade = getFacade(id);
      if (facade && input.draft.startsWith('/企业任务')) facade.setDraft(input.draft.replace(/^\/企业任务\s?/, ''));
      selections.delete(id); announce();
    };
    const clearTool = async key => {
      if (toolBusy || inflight.has(id)) return;
      setToolBusy(true); setError('');
      try { await selectTools(id, toolIds.filter(value => value !== key)); }
      catch (e) { setError(e.message); } finally { setToolBusy(false); }
    };
    return h('div', { className: 'mm-skill-launcher' },
      button(h(React.Fragment, null, icon('spark', 15), h('span', null, '技能'), toolIds.length ? h('span', { className: 'mm-skill-badge' }, toolIds.length) : null), { ref: trigger, onClick: () => setOpen(v => !v), disabled: !id || inflight.has(id) || toolBusy,
        'aria-haspopup': 'dialog', 'aria-label': '选择技能', 'aria-expanded': open, 'data-megmeet-plugin-picker': '', 'data-megmeet-plugin-chip': selected ? selected.plugin.plugin_key : undefined }),
      selected ? h('span', { className: 'mm-skill-chip' }, selected.plugin.name, h('button', { type: 'button', onClick: remove, disabled: inflight.has(id), 'aria-label': '移除所选技能' }, icon('close', 12))) : null,
      ...toolIds.map(key => h('span', { key, className: 'mm-skill-chip', 'data-megmeet-selected-skill': key, title: isWorkflow(key) ? '发送消息后执行此项目流程' : '模型根据当前问题按需使用' }, workflowNames[key] || toolStates.get(id)?.catalogNames?.[key] || ({ web: '联网搜索', dingtalk: '钉钉' })[key] || key,
        isWorkflow(key) ? h('span', { className: 'mm-skill-muted', style: { fontSize: 10 }, 'data-megmeet-workflow-selected': key }, '· 发送后执行') : null,
        h('button', { type: 'button', onClick: () => clearTool(key), disabled: toolBusy, 'aria-label': '取消技能 ' + key }, icon('close', 12)))),
      error ? h('small', { role: 'alert', className: 'mm-skill-muted', style: { maxWidth: 240 } }, error) : null,
      open ? h(PluginPopover, { key: id, sessionId: id, anchor: trigger, selected: [...toolIds, ...(selected ? [selected.plugin.plugin_key] : [])], onClose: () => setOpen(false), onChoose: choose }) : null);
  }

  const resultKey = (location, id) => `${location}:${id}`;
  async function loadResults(id, location) {
    const response = await api(`/plugins/results?native_session_id=${encodeURIComponent(id)}&location=${location}`);
    const items = Array.isArray(response.items) ? response.items : [];
    results.set(resultKey(location, id), items); announce(); return items;
  }
  function PluginResults({ sessionId, useSession }) {
    const session = useSession?.(s => s), id = sessionId || session?.id || session?.sessionId;
    const [location, setLocation] = useState(null), [error, setError] = useState('');
    observe();
    useEffect(() => {
      let live = true; setLocation(null); setError('');
      if (id) mode().then(async value => {
        if (!live) return;
        const current = value.mode; setLocation(current);
        await loadResults(id, current);
      }).catch(e => { if (live) setError(e.message); });
      return () => { live = false; };
    }, [id]);
    const items = location && results.get(resultKey(location, id)) || [];
    // Background loading must not put a plugin error panel in an ordinary
    // native chat. A selected plugin or known result makes this surface relevant.
    if (!items.length && !(error && selections.has(id))) return null;
    return h('section', { 'aria-label': '企业技能结果', 'data-megmeet-plugin-results': '', style: { ...dockStyle,border: '1px solid var(--dsw-alias-border-l2,#e3e8e0)', borderRadius: 16, padding: 16, marginBottom: 12, maxHeight: 320, overflowY: 'auto' } },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } }, h('strong', null, '企业技能结果'),
        button('刷新', { onClick: () => loadResults(id, location).then(() => setError('')).catch(e => setError(e.message)) })),
      h('small', { style: { opacity: .7 } }, '技能在云端处理，结果仅自己可见并保存在服务器；按当前会话关联。'),
      error ? h('p', { role: 'alert' }, error) : null,
      items.map(run => h('article', { key: run.run_id || run.request_id, style: { whiteSpace: 'pre-wrap', paddingTop: 8 } },
        h('strong', null, run.plugin_name || run.plugin_key || '企业技能'), h('p', null, run.input),
        h('p', null, run.result?.text || (run.status === 'running' ? '正在处理…' : `任务状态：${run.status}`)))));
  }
  // Hot settings updates feed the original DSH selector without touching drafts.
  let modelSync, modelSyncAt = 0, modelNotice = '';
  const modelObservers = new Set();
  function syncModels() {
    if (modelSync || Date.now() - modelSyncAt < 2000) return;
    if (workspaceContext?.get('connection')?.state?.getSnapshot() !== 'connected') return;
    modelSyncAt = Date.now();
    modelSync = request('/__megmeet/desktop/models/refresh', {})
      .then(value => { modelNotice = value.notice || ''; })
      .catch(() => { modelNotice = '模型列表暂未同步，请稍后重新打开窗口；发送时仍会核验模型权限。'; })
      .finally(() => { modelSync = undefined; modelObservers.forEach(fn => fn(modelNotice)); });
  }
  function ModelSync() {
    const [notice, setNotice] = useState(modelNotice);
    useEffect(() => {
      modelObservers.add(setNotice);
      const connection = workspaceContext?.get('connection')?.state;
      const onVisible = () => { if (document.visibilityState !== 'hidden') syncModels(); };
      const unsubscribe = connection?.subscribe?.(syncModels);
      window.addEventListener('focus', syncModels);
      document.addEventListener('visibilitychange', onVisible);
      const timer = setInterval(onVisible, 60000);
      syncModels();
      return () => {
        modelObservers.delete(setNotice); unsubscribe?.(); clearInterval(timer);
        window.removeEventListener('focus', syncModels);
        document.removeEventListener('visibilitychange', onVisible);
      };
    }, []);
    return notice ? h('small', { role: 'status', 'aria-label': notice, title: notice, 'data-megmeet-model-notice': '',
      style: { maxWidth: 130, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontSize: 10 } }, notice) : null;
  }
  async function submit(chosen, text, files, attempt, id, location, conversationId) {
    const plan = makePlan(chosen, text, files, attempt.id, id, location, conversationId);
    for (let i = 0; i < files.length; i++) {
      const file = files[i], bytes = new Uint8Array(await file.arrayBuffer());
      if (new TextDecoder('utf-8', { fatal: true }).decode(bytes).includes('\u0000')) throw Error('当前技能只接受 UTF-8 文本文件');
      const sha256 = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
      const signature = `${file.name}:${sha256}`;
      if (attempt.uploads[i]?.signature !== signature) {
        let binary = '';
        for (let j = 0; j < bytes.length; j += 8192) binary += String.fromCharCode(...bytes.subarray(j, j + 8192));
        const upload = await api('/plugins/uploads', { ...plan.scope, name: file.name, sha256, content_base64: btoa(binary) });
        attempt.uploads[i] = { signature, id: upload.attachment_id };
      }
      plan.body.attachment_ids.push(attempt.uploads[i].id);
    }
    return api(plan.endpoint, plan.body);
  }
  function claim(id, chosen) {
    return { name: '企业任务', token: '/企业任务 ', hint: chosen ? chosen.plugin.name : '请先选择授权技能', attachments: true,
      async submit(text, actx, attachments) {
        if (!chosen || selections.get(id) !== chosen) return { kind: 'error', text: '请重新选择企业技能。消息未发送。' };
        if (inflight.has(id)) return { kind: 'error', text: '此会话的技能任务正在处理。' };
        const conversation = actx.get('conversation');
        if (typeof conversation?.resolveDraftAttachments !== 'function') return { kind: 'error', text: '此 DSH 版本的附件接口未通过适配验证，消息未发送。' };
        const facade = conversation.input.for(actx), ids = facade.state.getSnapshot().attachmentIds;
        const originals = conversation.resolveDraftAttachments(ids);
        if (originals.length !== attachments.length || originals.some(x => !x.file)) return { kind: 'error', text: '附件来源已变化，请重新选择文件。' };
        const files = originals.map(x => x.file);
        if (files.some(f => !/\.(txt|md|csv|json)$/i.test(f.name) || f.size > 262144)) return { kind: 'error', text: '当前技能支持 txt、md、csv、json 文本，每个不超过 256 KiB。' };
        inflight.add(id); announce();
        try {
          const current = await mode(), location = current.mode;
          const signature = JSON.stringify([chosen.plugin.plugin_key, chosen.plugin.version, location, text, files.map(f => [f.name, f.size, f.lastModified])]);
          if (chosen.attempt?.signature !== signature) chosen.attempt = { id: crypto.randomUUID(), uploads: [], signature };
          const attempt = chosen.attempt;
          makePlan(chosen, text, files, attempt.id, id, location);
          if (!attempt.confirmed) {
            if (!window.confirm(`将本条消息${files.length ? `和以下 ${files.length} 个所选文件` : ''}交给“${chosen.plugin.name}”在服务器处理？\n${files.map(f => f.name).join('\n')}\n结果保存在服务器并关联当前会话，仅自己可见。其他聊天历史和目录不会上传。`)) return { kind: 'error', text: '已取消，消息和文件仍保留在输入框。' };
            attempt.confirmed = true;
          }
          if (!attempt.bound) {
            const previous = await loadResults(id, location);
            attempt.conversationId = [...previous].reverse().find(run => run.plugin_key === chosen.plugin.plugin_key && run.plugin_version === chosen.plugin.version)?.conversation_id;
            attempt.bound = true;
          }
          const result = attempt.result || await submit(chosen, text, files, attempt, id, location, attempt.conversationId);
          if (result.status === 'completed') attempt.result = result;
          await loadResults(id, location).catch(() => {
            const key = resultKey(location, id), items = results.get(key) || [];
            results.set(key, [...items.filter(x => x.request_id !== attempt.id), { ...result, request_id: attempt.id, input: text, plugin_name: chosen.plugin.name }]); announce();
          });
          if (result.status !== 'completed') return { kind: 'error', text: '任务尚未完成。重试会查询同一任务，不会重复创建。' };
          try { facade.notify('info', '企业技能已完成，结果显示在当前输入框上方。'); } catch {}
          chosen.attempt = null;
          return { kind: 'success' };
        } catch (e) { return { kind: 'error', text: e.message }; }
        finally { inflight.delete(id); announce(); }
      } };
  }
  function AccountActions({ wide = true }) {
    const [web, setWeb] = useState(false), [open, setOpen] = useState(false), [state, setState] = useState({ status: 'ready', busy: false }), [error, setError] = useState('');
    const root = useRef(null), trigger = useRef(null), menu = useRef(null);
    const floating = useFloating(trigger, menu, open, 212);
    useEffect(() => {
      let alive = true;
      mode().then(value => { if (alive) setWeb(value.client === 'web'); }).catch(() => {});
      const bridge = globalThis.__MEGMEET_WEB_SESSION__;
      if (bridge) setState(bridge.getSnapshot());
      const release = bridge?.subscribe(value => { if (alive) setState(value); });
      return () => { alive = false; release?.(); };
    }, []);
    useEffect(() => {
      if (!open) return;
      const close = event => {
        if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
        else if (event.type === 'pointerdown' && !root.current?.contains(event.target) && !menu.current?.contains(event.target)) setOpen(false);
      };
      window.addEventListener('keydown', close); window.addEventListener('pointerdown', close);
      return () => { window.removeEventListener('keydown', close); window.removeEventListener('pointerdown', close); };
    }, [open]);
    if (!web) return null;
    const label = state.status === 'expired' ? '重新登录' : state.status === 'released' ? '重新进入云端' : '个人云端';
    return h('div', { ref: root, 'data-megmeet-web-account': '', style: { position: 'relative', width: '100%', minWidth: 0, padding: '2px 0' } },
      button(h(React.Fragment, null, h('span', { 'aria-hidden': true, style: { width: 7, height: 7, borderRadius: '50%', background: state.status === 'ready' ? '#40af9f' : '#ad9870', flex: 'none' } }), wide ? h('span', null, label) : null),
        { ref: trigger, onClick: () => setOpen(value => !value), 'aria-label': '云端账号与退出', 'aria-haspopup': 'menu', 'aria-expanded': open, title: label,
          style: { ...buttonStyle, border: 0, borderRadius: 12, display: 'flex', alignItems: 'center', gap: 9, width: wide ? '100%' : 36, height: 36, padding: wide ? '7px 12px' : 0, justifyContent: wide ? 'flex-start' : 'center', color: 'var(--dsw-alias-label-secondary,#69796f)', textAlign: 'left' } }),
      open ? createPortal(h('div', { ref: menu, role: 'menu', 'aria-label': '云端账号', className: 'mm-skill-popover', style: { ...floating, overflowY: 'auto', padding: 7, zIndex: 90 } },
        h('div', { className: 'mm-skill-muted', style: { padding: '7px 9px 10px', borderBottom: '1px solid var(--dsw-alias-border-l2,#e3e8e0)', marginBottom: 4 } }, state.busy ? '模型正在处理任务' : '网页版 · 个人云端'),
        state.status !== 'ready' ? h('button', { role: 'menuitem', className: 'mm-skill-row', onClick: () => globalThis.__MEGMEET_WEB_SESSION__?.resume() }, label) : null,
        h('a', { href: '/maimi/', role: 'menuitem', className: 'mm-skill-row', style: { boxSizing: 'border-box', textDecoration: 'none' } }, '返回平台首页'),
        h('button', { role: 'menuitem', className: 'mm-skill-row', disabled: state.status === 'signing_out', onClick: async () => {
          setError('');
          try { if (!globalThis.__MEGMEET_WEB_SESSION__) throw Error(); await globalThis.__MEGMEET_WEB_SESSION__.logout(); }
          catch { setError('退出失败，请重试。'); }
        } }, state.status === 'signing_out' ? '正在退出…' : '退出登录'),
        error ? h('p', { role: 'alert', className: 'mm-skill-muted', style: { margin: '4px 9px 8px' } }, error) : null), document.body) : null);
  }

  ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({ name: 'sidebar.footer.action', id: 'megmeet-web-account', order: 0 }, AccountActions));
  ctx.slots.inject('sidebar.workspaces.pinned', () => ctx.slots.register({ name: 'sidebar.workspaces.pinned', id: 'megmeet-personal-cloud' }, PinnedWorkspace));
  ctx.inject(['connection', 'workspaces', 'uiWorkspace'], scoped => { workspaceContext = scoped; });
  ctx.inject(['conversation', 'sessions', 'inputTriggers'], scoped => {
    commandContext = scoped;
    scoped.slots.inject('conversation.input.left', () => scoped.slots.register({ name: 'conversation.input.left', id: 'megmeet-enterprise', order: 20 }, Launcher));
    scoped.slots.inject('conversation.input.dock', () => scoped.slots.register({ name: 'conversation.input.dock', id: 'megmeet-enterprise-results', order: 20 }, EnterpriseDock));
    scoped.slots.inject('conversation.input.right', () => scoped.slots.register({ name: 'conversation.input.right', id: 'megmeet-model-sync', order: 20 }, ModelSync));
    scoped.inputTriggers.registerSource({ trigger: '/', name: 'megmeet-enterprise', order: -100, candidates: () => [], onPick: () => undefined,
      matchEnter(session, line) {
        if (!/^\/企业任务(?:\s|$)/u.test(line)) return undefined;
        return { claim: claim(session.sessionId, selections.get(session.sessionId)) };
      } });
  });
  ctx.on('dispose', () => { selections.clear(); results.clear(); toolStates.clear(); listeners.clear(); modelObservers.clear(); });
}
