// MEGMEET additions occupy native DSH slots. Local conversations, files and
// workspace navigation retain DSH ownership; plugin execution is explicit.
export function createRequestPlan(selection, input, files, requestId, sessionId, location, conversationId) {
  const uuid = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const nativeId = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
  const plugin = selection?.plugin;
  if (!plugin || !/^[a-z0-9][a-z0-9._-]{0,99}$/i.test(plugin.plugin_key) || !plugin.version) throw Error('请选择已授权的企业插件');
  if (!uuid.test(requestId) || !nativeId.test(sessionId) || !['local', 'cloud'].includes(location)) throw Error('当前会话信息无效');
  if (conversationId && !uuid.test(conversationId)) throw Error('插件会话信息无效');
  if (!input.trim() || input.length > 20000) throw Error('请输入消息，最多 20000 字');
  if (files.length > 16 || files.some(f => f.size > 262144)) throw Error('最多 16 个 UTF-8 文本文件，每个不超过 256 KiB');
  const body = { request_id: requestId, plugin_key: plugin.plugin_key, plugin_version: plugin.version,
    native_session_id: sessionId, location, input, attachment_ids: [], ...(conversationId ? { conversation_id: conversationId } : {}) };
  const scope = { request_id: requestId, plugin_key: plugin.plugin_key, plugin_version: plugin.version, explicit_upload: true };
  return { endpoint: '/plugins/tasks', body, scope };
}

// Serialized by the version-guarded brand adapter; keep this function closed
// over its parameters and browser globals, not module-scope helpers.
export function installEnterprise(ctx, require, makePlan) {
  const React = require('react');
  const { createElement: h, useEffect, useRef, useState } = React;
  const selections = new Map(), results = new Map(), listeners = new Set(), inflight = new Set();
  const toolStates=new Map();
  let commandContext, workspaceContext;
  const announce = () => listeners.forEach(fn => fn());
  const errors = { 401: '登录已过期，请重新登录。', 403: '你尚未获得此插件的使用权限。', 409: '会话或任务状态已变化，草稿已保留，请重试。', 503: '服务暂时不可用，请保留消息后重试。' };
  async function request(url, body) {
    const response = await fetch(url, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(120000) });
    if (!response.ok) throw Error(errors[response.status] || `请求未完成 (${response.status})`);
    return response.json();
  }
  const api = (path, body) => request('/__megmeet/enterprise' + path, body);
  const mode = () => request('/__megmeet/desktop/workspace');
  const cloudIntentKey = 'megmeet.explicit-cloud-open.v1';
  const buttonStyle = { border: '1px solid #8884', borderRadius: 8, padding: '5px 9px', background: 'transparent', color: 'inherit', cursor: 'pointer', font: 'inherit' };
  const button = (label, props = {}) => h('button', { type: 'button', style: buttonStyle, ...props }, label);
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
          if (result.status !== 'queued') break;
          if (!alive.current) return;
          if (Date.now() >= deadline) throw Error('云端仍繁忙，已结束本次排队。可以继续本机聊天，稍后再试。');
          setQueued(result.position);
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
        title: '个人云端工作区 · 数据保存在服务器 · 固定保留', 'data-megmeet-cloud-open': '',
        style: { ...buttonStyle, textAlign: wide ? 'left' : 'center', width: '100%', borderColor: state.mode === 'cloud' ? '#3b82f6' : '#8883' } }),
      state.mode === 'cloud' && state.client !== 'web' ? button(wide ? '返回本机工作区' : '⌂', { onClick: () => open('local'), disabled: busy, 'aria-label': '返回本机工作区', 'data-megmeet-local-open': '' }) : null,
      wide ? h(HistoryTrash) : null,
      queued ? h('small', {role:'status'}, `云端繁忙，正在排队（第 ${queued} 位）`) : null,
      error ? h('small', { role: 'alert' }, error) : null);
  }
  function HistoryTrash() {
    const [open, setOpen] = useState(false), [items, setItems] = useState([]), [message, setMessage] = useState(''), [busy, setBusy] = useState(false), [next, setNext] = useState(null);
    const load = async (page=0) => {
      setBusy(true); setMessage('');
      try { const value = await api('/personal-cloud/history/trash'+(page ? '?page='+page : '')); setItems(list => page ? [...list,...(value.items || [])] : value.items || []); setNext(value.next_page ?? null); }
      catch (e) { setMessage(e.message); } finally { setBusy(false); }
    };
    const restore = async session_id => {
      setBusy(true);
      try {
        await api('/personal-cloud/history/restore', {session_id});
        setItems(list => list.map(row => row.session_id === session_id ? {...row, restore_requested_at: true} : row));
        setMessage('恢复已排队。请关闭云端聊天页面，云端闲置后将自动恢复。');
      } catch (e) { setMessage(e.message); } finally { setBusy(false); }
    };
    return h('div', null,
      button('云端历史回收站', {onClick: () => {setOpen(!open); if (!open) void load();}, 'aria-expanded':open,
        style:{...buttonStyle,fontSize:11,padding:'3px 6px',opacity:.8}}),
      open ? h('div', {'data-megmeet-history-trash':'',style:{padding:8,maxHeight:260,overflowY:'auto',fontSize:12,border:'1px solid #8884',borderRadius:8}},
        h('p', {style:{margin:'0 0 6px'}}, '90 天未活动的聊天在这里保留 30 天。项目文件不随聊天清理。'),
        busy ? h('small', null, '正在处理…') : null,
        !items.length && !busy ? h('small', null, '暂无可恢复的聊天') : null,
        items.map(row => h('div', {key:row.session_id,style:{padding:'6px 0',borderTop:'1px solid #8883'}},
          h('div', {style:{wordBreak:'break-word'}}, row.title || '未命名聊天'),
          h('small', null, `保留至 ${new Date(row.purge_after).toLocaleDateString()}`),
          button(row.restore_requested_at ? '等待恢复' : '恢复', {disabled:busy || !!row.restore_requested_at,
            onClick: () => restore(row.session_id),style:{...buttonStyle,marginLeft:6,padding:'2px 6px'}}))),
        next !== null ? button('更多历史', {onClick:()=>load(next),disabled:busy}) : null,
        message ? h('p', {role:'status'}, message) : null) : null);
  }
  function PluginPopover({ onChoose, onClose }) {
    const [plugins, setPlugins] = useState(null), [error, setError] = useState('');
    const root = useRef(null);
    useEffect(() => {
      let live = true;
      Promise.allSettled([api('/plugins'),request('/__megmeet/tools/catalog')]).then(values=>{
        if(!live)return;
        const legacy=values[0].status==='fulfilled'?(values[0].value.items||[]):[];
        const tools=values[1].status==='fulfilled'?(values[1].value.items||[]).map(p=>({...p,connector:true})):[];
        setPlugins([...tools,...legacy]);
        if(values.every(v=>v.status==='rejected'))setError('插件暂时不可用，请稍后重试。');
      });
      const close = e => { if (e.key === 'Escape' || (e.type === 'pointerdown' && root.current && !root.current.contains(e.target))) onClose(); };
      window.addEventListener('keydown', close); window.addEventListener('pointerdown', close);
      return () => { live = false; window.removeEventListener('keydown', close); window.removeEventListener('pointerdown', close); };
    }, []);
    return h('div', { ref: root, role: 'menu', 'aria-label': '选择企业插件', 'data-megmeet-plugin-popover': '',
      style: { position: 'absolute', bottom: 'calc(100% + 8px)', left: 0, width: 280, maxWidth: '75vw', maxHeight: 300,
        overflowY: 'auto', zIndex: 80, padding: 8, border: '1px solid #8885', borderRadius: 12, background: 'Canvas', color: 'CanvasText', boxShadow: '0 8px 30px #0002' } },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '2px 4px 6px' } }, h('strong', null, '企业插件'), button('×', { onClick: onClose, 'aria-label': '关闭插件选择' })),
      error ? h('small', { role: 'alert' }, error) : plugins === null ? h('small', null, '正在加载…') : !plugins.length ? h('small', null, '暂无已授权的插件') :
        plugins.map(plugin => h('button', { key: `${plugin.plugin_key}@${plugin.version}`, type: 'button', role: 'menuitem',
          'data-megmeet-plugin-key': plugin.plugin_key, disabled:plugin.connector&&!['ready','available','limited','authorization_required'].includes(plugin.status),
          onClick: () => {
            if(plugin.connector&&plugin.status==='authorization_required'){
              try{const u=new URL(plugin.authorization_url);if(u.protocol==='https:'||u.origin===window.location.origin)window.open(u.href,'_blank','noopener,noreferrer');}catch{}
              return;
            }
            onChoose({ plugin: { ...plugin } });
          },
          style: { ...buttonStyle, width: '100%', textAlign: 'left', marginBottom: 4 } },
        h('span', null, plugin.name), plugin.description ? h('small', { style: { display: 'block', opacity: .7, paddingTop: 3 } }, plugin.description) : null,
        plugin.connector&&plugin.status==='authorization_required'?h('small',null,'点击关联本人钉钉账号'):null,
        plugin.connector&&plugin.reason?h('small',{style:{display:'block',opacity:.7}},plugin.reason):null)));
  }
  async function readToolState(id){const value=await request('/__megmeet/tools/state?session_id='+encodeURIComponent(id));toolStates.set(id,value);announce();return value;}
  async function selectTools(id,plugins){const value=await request('/__megmeet/tools/selection',{session_id:id,plugins});toolStates.set(id,{...value,pending:[]});announce();}
  const dockStyle={boxSizing:'border-box',width:'calc(100% - var(--dsh-composer-side-clearance, 24px) - var(--dsh-composer-side-clearance, 24px))',maxWidth:'var(--dsh-composer-card-max-width, 780px)',margin:'0 auto',background:'var(--dsw-alias-bg-base, Canvas)',color:'var(--dsw-alias-label-primary, CanvasText)'};
  function previewFields(preview){
    const labels={text:'消息内容',title:'标题',description:'说明',start:'开始时间',end:'结束时间',due_time_ms:'截止时间',recipients:'收件人',attendees:'参与人'};
    return Object.entries(preview?.arguments||{}).map(([key,value])=>{
      let content;
      if((key==='recipients'||key==='attendees')&&Array.isArray(value)){
        content=value.map(no=>{const person=preview.recipients?.find(p=>p.employee_no===no);return person?`${person.name}（${no}）`:String(no);}).join('、')||'无';
      }else if(key==='due_time_ms'&&typeof value==='number')content=new Date(value).toLocaleString();
      else content=typeof value==='string'?value:JSON.stringify(value);
      return h('div',{key,style:{marginTop:6}},h('strong',null,(labels[key]||key)+'：'),h('span',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},content));
    });
  }
  function ToolPending({sessionId,useSession}){
    const session=useSession?.(s=>s),id=sessionId||session?.id||session?.sessionId;
    const [message,setMessage]=useState(''),[busy,setBusy]=useState(false);observe();
    const pending=toolStates.get(id)?.pending||[];
    useEffect(()=>{setMessage('');},[id]);
    const confirm=async(item,confirmed)=>{
      setBusy(true);setMessage('');
      try{
        const value=await request('/__megmeet/tools/confirm',{session_id:id,confirmation_id:item.confirmation_id,confirmed});
        const message=!confirmed?'已取消操作。':value.status==='completed'?'操作已完成。':'已收到操作结果，请核对下方状态。';
        setMessage(message+(confirmed?'\n'+JSON.stringify(value.result||value,null,2).slice(0,4000):''));
        await readToolState(id);getFacade(id)?.notify('info',message);
      }catch(e){setMessage(e.message);}finally{setBusy(false);}
    };
    if(!pending.length&&!message)return null;
    return h('div',{'data-megmeet-tool-confirmations':'',role:'region','aria-label':'操作确认',style:{...dockStyle,border:'1px solid #8885',borderRadius:10,padding:12,maxHeight:260,overflow:'auto'}},
      pending.map(item=>h('div',{key:item.confirmation_id},h('strong',null,item.plugin_name+' · 等待确认'),
        h('p',{style:{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}},item.preview?.summary||'请核对以下操作'),
        h('div',{style:{marginBottom:10,fontSize:13}},...previewFields(item.preview)),
        button('确认执行',{onClick:()=>confirm(item,true),disabled:busy}), ' ',button('取消',{onClick:()=>confirm(item,false),disabled:busy}))),
      message?h('p',{role:'status'},message):null);
  }
  function EnterpriseDock(props){return h(React.Fragment,null,h(PluginResults,props),h(ToolPending,props));}
  function Launcher({ useSession, useInput, sessionId }) {
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
          const next=toolIds.includes(selection.plugin.plugin_key)?toolIds:[...toolIds,selection.plugin.plugin_key];
          await selectTools(id,next);
          const current=facade.state.getSnapshot();
          if(!toolIds.includes(selection.plugin.plugin_key)&&current.phase!=='submitting')facade.setDraft(`【插件：${selection.plugin.name}】 `+current.draft);
          setOpen(false);
        }catch(e){setError(e.message);}finally{setToolBusy(false);}
        return;
      }
      const existing = selections.get(id);
      const entry = { plugin: { ...selection.plugin }, token: '/企业任务 ', attempt: null };
      if (facade.beginCommand(claim(id, entry), { start: 0, end: existing && input.draft.startsWith(existing.token) ? existing.token.length : 0, draftRev: input.draftRev })) {
        selections.set(id, entry); announce(); setOpen(false);
      } else facade.notify('error', '当前输入正在处理，请稍后选择插件。');
    };
    const selected = id && input?.draft?.startsWith('/企业任务') && selections.get(id);
    const remove = () => {
      if (inflight.has(id) || input?.phase === 'submitting') return;
      const facade = getFacade(id);
      if (facade && input.draft.startsWith('/企业任务')) facade.setDraft(input.draft.replace(/^\/企业任务\s?/, ''));
      selections.delete(id); announce();
    };
    return h('div', { style: { position: 'relative', display: 'inline-flex', gap: 4, alignItems: 'center' } },
      button(selected ? selected.plugin.name : toolIds.length?`插件 (${toolIds.length})`:'插件', { onClick: () => setOpen(v => !v), disabled: !id || inflight.has(id)||toolBusy,
        'aria-haspopup': 'menu', 'aria-expanded': open, 'data-megmeet-plugin-picker': '', 'data-megmeet-plugin-chip': selected ? selected.plugin.plugin_key : undefined }),
      selected ? button('×', { onClick: remove, disabled: inflight.has(id), 'aria-label': '移除所选插件' }) : null,
      toolIds.length?button('停止工具',{onClick:async()=>{try{await request('/__megmeet/tools/stop',{session_id:id});toolStates.set(id,{selected:[],pending:[]});announce();}catch(e){setError(e.message);}},'data-megmeet-tool-stop':''}):null,
      error?h('small',{role:'alert',style:{maxWidth:160,fontSize:11}},error):null,
      open ? h(PluginPopover, { key: id, onClose: () => setOpen(false), onChoose: choose }) : null);
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
    return h('section', { 'aria-label': '企业插件结果', 'data-megmeet-plugin-results': '', style: { ...dockStyle,border: '1px solid #8884', borderRadius: 10, padding: 10, marginBottom: 8, maxHeight: 320, overflowY: 'auto' } },
      h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center' } }, h('strong', null, '企业插件结果'),
        button('刷新', { onClick: () => loadResults(id, location).then(() => setError('')).catch(e => setError(e.message)) })),
      h('small', { style: { opacity: .7 } }, '插件在服务器执行，结果仅自己可见并保存在服务器；按当前会话关联。'),
      error ? h('p', { role: 'alert' }, error) : null,
      items.map(run => h('article', { key: run.run_id || run.request_id, style: { whiteSpace: 'pre-wrap', paddingTop: 8 } },
        h('strong', null, run.plugin_name || run.plugin_key || '企业插件'), h('p', null, run.input),
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
      if (new TextDecoder('utf-8', { fatal: true }).decode(bytes).includes('\u0000')) throw Error('当前插件只接受 UTF-8 文本文件');
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
    return { name: '企业任务', token: '/企业任务 ', hint: chosen ? chosen.plugin.name : '请先选择授权插件', attachments: true,
      async submit(text, actx, attachments) {
        if (!chosen || selections.get(id) !== chosen) return { kind: 'error', text: '请重新选择企业插件。消息未发送。' };
        if (inflight.has(id)) return { kind: 'error', text: '此会话的插件任务正在处理。' };
        const conversation = actx.get('conversation');
        if (typeof conversation?.resolveDraftAttachments !== 'function') return { kind: 'error', text: '此 DSH 版本的附件接口未通过适配验证，消息未发送。' };
        const facade = conversation.input.for(actx), ids = facade.state.getSnapshot().attachmentIds;
        const originals = conversation.resolveDraftAttachments(ids);
        if (originals.length !== attachments.length || originals.some(x => !x.file)) return { kind: 'error', text: '附件来源已变化，请重新选择文件。' };
        const files = originals.map(x => x.file);
        if (files.some(f => !/\.(txt|md|csv|json)$/i.test(f.name) || f.size > 262144)) return { kind: 'error', text: '当前插件支持 txt、md、csv、json 文本，每个不超过 256 KiB。' };
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
          try { facade.notify('info', '企业插件已完成，结果显示在当前输入框上方。'); } catch {}
          chosen.attempt = null;
          return { kind: 'success' };
        } catch (e) { return { kind: 'error', text: e.message }; }
        finally { inflight.delete(id); announce(); }
      } };
  }
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
