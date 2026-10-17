"""Browser entry for the employee's existing native cloud Host and data."""
import re
import httpx
from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from . import personal_cloud as cloud
from . import iam_oauth as iam
from .personal_cloud_images import router as image_router
from .personal_cloud_files import router as file_router
from .personal_cloud_preview import render as render_preview

router=APIRouter()
router.include_router(image_router)
router.include_router(file_router)
PUBLIC_WEB_PATHS=frozenset(('/maimi/web/','/maimi/web/entry.js','/maimi/web/session.js','/maimi/web/upload.js','/maimi/web/image.js','/maimi/web/files.js'))
NATIVE_ROOT_PREFIXES=('/api/','/plugins/','/assets/','/ext/')
HEADERS={**cloud.HEADERS,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'}
WEB_STATE={'mode':'cloud','client':'web','cloud':{'name':'云端工作区','pinned':True,'deletable':False,'manual':False}}

@router.get('/maimi/web/')
async def entry(request:Request):
    auto_login = request.cookies.get('megmeet_web_signed_out') != '1'
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>麦米网页版</title><link rel="stylesheet" href="/maimi/assets/app.css"><script src="/maimi/web/entry.js" defer></script><body data-auto-login="__AUTO_LOGIN__"><header><span class="wordmark">MEGMEET</span><span>麦米网页版</span></header><main><h1>进入个人云端工作区</h1><p>网页版与桌面版共享你的云端聊天和文件。</p><p id="state" role="status">正在检查登录…</p><a id="login" class="download" hidden href="/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F">通过公司 IAM 登录</a><button id="retry" class="download" hidden>重新连接</button><p><a href="/maimi/">返回首页</a></p></main></body></html>'''.replace('__AUTO_LOGIN__', 'true' if auto_login else 'false'),headers={**HEADERS,'Content-Security-Policy':"default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

@router.get('/maimi/web/entry.js')
async def entry_script():
    return Response("const status = document.getElementById('state');\nconst login = document.getElementById('login');\nconst retry = document.getElementById('retry');\nconst loginURL = '/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F';\nconst attemptKey = 'megmeet.web.iam-attempt.v1';\nlet active = false;\n\nfunction forgetAttempt() {\n  try { sessionStorage.removeItem(attemptKey); } catch {}\n}\n\nfunction requireLogin() {\n  login.hidden = false;\n  status.textContent = '请通过公司 IAM 登录，已登录的账号将尝试自动进入。';\n  if (document.body.dataset.autoLogin !== 'true') return;\n  // Remember one navigation in this tab. Failed callbacks, blocked cookies,\n  // or storage failures must not send the browser through a redirect loop.\n  try {\n    const raw = sessionStorage.getItem(attemptKey);\n    const attempted = raw === null ? NaN : Number(raw);\n    const now = Date.now();\n    if (Number.isFinite(attempted) && (attempted > now || now - attempted < 120000)) return;\n    sessionStorage.setItem(attemptKey, String(now));\n    if (sessionStorage.getItem(attemptKey) !== String(now)) return;\n  } catch { return; }\n  status.textContent = '正在通过公司 IAM 确认登录…';\n  location.replace(loginURL);\n}\n\nlogin.onclick = () => { forgetAttempt(); };\n\nasync function enter() {\n  if (active) return;\n  active = true;\n  retry.hidden = true;\n  login.hidden = true;\n  const until = Date.now() + 120000;\n  try {\n    // /open performs the same IAM validation and reuses a ready Host directly.\n    while (Date.now() < until) {\n      status.textContent = '正在连接个人云端工作区…';\n      const response = await fetch('/v1/personal-cloud/open', {\n        method: 'POST', headers: {'Content-Type': 'application/json'},\n        body: '{}', credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(15000),\n      });\n      if (response.status === 401) { requireLogin(); return; }\n      if (response.status === 403) {\n        status.textContent = '当前账号暂时无法使用麦米，请联系管理员确认授权。';\n        return;\n      }\n      if (response.status === 202) {\n        const value = await response.json();\n        if (value.status === 'starting' && value.phase === 'preparing') {\n          status.textContent = '正在准备云端工作区，请稍候…';\n          await new Promise(resolve => setTimeout(resolve, 1000));\n        } else if (value.status === 'queued' && Number.isInteger(value.position)\n            && value.position >= 1) {\n          status.textContent = '云端工作区正在排队（第 ' + value.position + ' 位），空闲后会自动进入。';\n          await new Promise(resolve => setTimeout(resolve, 5000));\n        } else { throw Error(); }\n        continue;\n      }\n      if (!response.ok) throw Error();\n      forgetAttempt();\n      location.replace('/maimi/web/chat');\n      return;\n    }\n    status.textContent = '本次等待已结束。你的登录仍保留，可点击重新连接。';\n    retry.hidden = false;\n  } catch {\n    status.textContent = '暂时无法连接云端，请稍后重试。';\n    retry.hidden = false;\n  } finally { active = false; }\n}\n\nretry.onclick = enter;\nif (new URLSearchParams(location.search).get('idle') === '1') {\n  status.textContent = '云端工作区已释放。点击继续，重新进入你的聊天和文件。';\n  retry.textContent = '继续使用';\n  retry.hidden = false;\n} else { void enter(); }\n",media_type='text/javascript',headers=HEADERS)

@router.get('/v1/personal-cloud/web/session')
async def session(request:Request):
    actor=await cloud.actor_for(request)
    from . import persistent_login
    response = JSONResponse({'user':actor.public(),'client':'web','mode':'cloud'},headers=HEADERS)
    return persistent_login.refresh_cookie(response, request, iam.IAMConfig.from_env().secure_cookie)

@router.get('/maimi/web/chat')
async def chat(request:Request):
    try:
        upstream=await cloud.native_http(request,'')
    except iam.IAMFailure as exc:
        # A maintenance restart or idle release invalidates the Host, not the
        # employee login. Re-enter through the existing authenticated open flow.
        if exc.status==409 and exc.code=='personal_cloud_open_required':
            return RedirectResponse('/maimi/web/',status_code=303,headers=HEADERS)
        raise
    if upstream.status_code!=200 or 'text/html' not in upstream.headers.get('content-type',''):return upstream
    content=bytearray()
    async for chunk in upstream.body_iterator:
        content.extend(chunk if isinstance(chunk,bytes) else chunk.encode())
        if len(content)>2*1024*1024:raise iam.IAMFailure(502,'native_index_invalid')
    if upstream.background:await upstream.background()
    html=content.decode('utf8')
    if '</head>' not in html:raise iam.IAMFailure(502,'native_index_invalid')
    # DSH 0.1.7 renders <base href="./"> and relative frontend assets.
    # This HTML is served at /maimi/web/chat; its native API/assets live at
    # the authenticated origin root. Fix the base before any asset is parsed.
    bases=re.findall(r'<base\b[^>]*>',html,flags=re.I)
    if bases:
        if len(bases)!=1 or bases[0] not in ('<base href="./">','<base href="/">'):
            raise iam.IAMFailure(502,'native_index_invalid')
        html=html.replace(bases[0],'<base href="/">',1)
    else:
        html,count=re.subn(r'<head(?:\s[^>]*)?>',lambda match:match.group(0)+'<base href="/">',html,count=1,flags=re.I)
        if count!=1:raise iam.IAMFailure(502,'native_index_invalid')
    html=html.replace('</head>','<script src="/maimi/web/upload.js"></script><script src="/maimi/web/session.js"></script><script src="/maimi/web/image.js"></script><script src="/maimi/web/files.js"></script></head>',1)
    return HTMLResponse(html,headers=HEADERS)

@router.get('/maimi/web/upload.js')
async def upload_script():
    # DSH selects this supported transport hook before Cordis boots. The gateway
    # must see filename/size in the browser's original HTTPS request, not merely
    # on a later internal proxy hop. Keep DSH's raw body and receipt protocol.
    return Response('''(()=>{
'use strict';
const uploadPath='/api/session/uploadFileBinary',maximumBytes=32*1024*1024;
function abortIfNeeded(signal){if(signal?.aborted)throw signal.reason??new DOMException('The operation was aborted.','AbortError');}
function cancelReader(reader,reason){try{void reader.cancel(reason).catch(()=>{});}catch{}}
async function collectBody(body,signal){
  abortIfNeeded(signal);
  if(body instanceof Blob){
    if(body.size>maximumBytes)throw new Error('文件超过32 MiB上传限制。');
    return body;
  }
  if(!(body instanceof ReadableStream))throw new TypeError('不支持的上传文件类型。');
  const reader=body.getReader(),parts=[];let size=0,completed=false;
  const onAbort=()=>cancelReader(reader,signal?.reason);
  signal?.addEventListener('abort',onAbort,{once:true});
  try{
    while(true){
      abortIfNeeded(signal);
      const item=await reader.read();
      abortIfNeeded(signal);
      if(item.done){completed=true;break;}
      if(!(item.value instanceof Uint8Array))throw new TypeError('上传文件流包含无效内容。');
      size+=item.value.byteLength;
      if(size>maximumBytes)throw new Error('文件超过32 MiB上传限制。');
      // Copy now: a producer may reuse and mutate a chunk after enqueueing it.
      parts.push(item.value.slice());
    }
    return new Blob(parts,{type:'application/octet-stream'});
  }finally{
    signal?.removeEventListener('abort',onAbort);
    if(!completed)cancelReader(reader);
    try{reader.releaseLock();}catch{}
  }
}
globalThis.__DSH_FILE_UPLOAD__={fetch:async(input,init={})=>{
  // DSH 0.1.7 sends document-relative upload routes; honor the same base as its assets.
  const url=new URL(input,document.baseURI);
  if(url.origin!==location.origin||url.protocol!=='https:'||(url.port!==''&&url.port!=='443')||url.username||url.password||url.pathname!==uploadPath||url.hash||(init.method??'GET').toUpperCase()!=='POST')throw new Error('文件上传必须通过本站HTTPS 443入口。');
  const headers=new Headers(init.headers);
  if(headers.get('content-type')?.split(';',1)[0].trim().toLowerCase()!=='application/octet-stream')throw new TypeError('文件上传格式无效。');
  const body=await collectBody(init.body,init.signal);
  abortIfNeeded(init.signal);
  const filename=url.searchParams.get('name')||(typeof init.body?.name==='string'&&init.body.name)||'attachment.bin';
  url.searchParams.set('filename',filename);
  url.searchParams.set('size',String(body.size));
  const options={...init,headers,body,credentials:init.credentials??'same-origin',mode:'same-origin',redirect:'error',cache:'no-store'};
  delete options.duplex;
  // Resolve fetch at call time so the separate r15 idle-session guard remains
  // in control. Do not capture a native fetch that would bypass that guard.
  return window.fetch(url,options);
}};
})();''',media_type='text/javascript',headers=HEADERS)

@router.get('/maimi/web/session.js')
async def session_script():
    # Logout is local to this web session. Never follow IAM's global logout URL,
    # which could also invalidate the independently authorized desktop client.
    return Response("/* r47: own authenticated Host recovery, independent of translated UI text. */\n(function installCloudRecovery(target) {\n  'use strict';\n  if (target.__MEGMEET_CREATE_CLOUD_RECOVERY__) return;\n  target.__MEGMEET_CREATE_CLOUD_RECOVERY__ = function createCloudRecovery(options) {\n    const now = options.now || Date.now;\n    const later = options.setTimeout || target.setTimeout.bind(target);\n    const cancel = options.clearTimeout || target.clearTimeout.bind(target);\n    const key = 'megmeet.cloud.recovery.r47';\n    const windowMs = 300000, listeners = new Set();\n    let state = Object.freeze({status:'ready', busy:false}), disposed = false, paused = false;\n    let pending = null, hooks = null, unsubscribe = null;\n    let connectionTimer = null, retryTimer = null, refreshTimer = null, presenceTimer = null;\n    let presencePending = false, generation = 0, busyCheckedAt = 0;\n    let budget = {started:now(), attempts:0, refreshes:0}, durable = false;\n    try {\n      const saved = JSON.parse(options.storage?.getItem(key) || 'null');\n      if (saved && Number.isFinite(saved.started) && Number.isInteger(saved.attempts)\n          && Number.isInteger(saved.refreshes) && saved.attempts >= 0 && saved.refreshes >= 0\n          && now() >= saved.started && now()-saved.started < windowMs) budget = saved;\n    } catch {}\n    function publish(value) {\n      state = Object.freeze({...state,...value});\n      for (const listener of listeners) { try { listener(state); } catch {} }\n    }\n    function storeBudget() {\n      durable = false;\n      try { const value=JSON.stringify(budget); options.storage?.setItem(key,value); durable=options.storage?.getItem(key)===value; } catch {}\n    }\n    function resetOldBudget() {\n      if (now() < budget.started || now()-budget.started >= windowMs) budget={started:now(),attempts:0,refreshes:0};\n    }\n    function terminal() { return ['expired','blocked','signing_out'].includes(state.status); }\n    function clearTimers() {\n      for (const timer of [connectionTimer,retryTimer,refreshTimer,presenceTimer]) if(timer!==null)cancel(timer);\n      connectionTimer=retryTimer=refreshTimer=presenceTimer=null;\n    }\n    function auth(status) {\n      if (![401,403].includes(status) || disposed) return false;\n      generation++; clearTimers();\n      publish({status:status===401?'expired':'blocked',busy:false});\n      options.onAuth?.(status);\n      return true;\n    }\n    function active() {return !disposed&&!paused&&!terminal();}\n    async function request(path, body, keepalive=false) {\n      const controller = new AbortController();\n      let timeout;\n      const expired=new Promise((resolve,reject)=>{timeout=later(()=>{controller.abort();reject(Error('CONTROL_TIMEOUT'));},15000);});\n      try {return await Promise.race([expired,(async()=>{\n        const response=await options.request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin',cache:'no-store',signal:controller.signal,...(keepalive?{keepalive:true}:{})});\n        // Keep the timeout active through body parsing; headers alone are not completion.\n        const value=response.ok&&response.status!==204?await response.json():null;\n        return {status:response.status,ok:response.ok,value};\n      })()]);}\n      finally {cancel(timeout);}\n    }\n    async function presence(present=true) {\n      if (disposed||terminal()||(present&&(paused||presencePending))) return;\n      if(present)presencePending=true;\n      const current=generation;\n      try {\n        const response=await request(options.presenceURL,{client_id:options.clientId,present},!present);\n        if (disposed||paused||!present||current!==generation) return;\n        if(auth(response.status))return;\n        if(response.status===409)void recover('host_missing');\n        else if(response.ok&&typeof response.value?.busy==='boolean'){busyCheckedAt=now();publish({busy:response.value.busy});}\n      } catch {} finally {if(present)presencePending=false;}\n    }\n    function schedulePresence() {\n      if(!active())return;\n      if(presenceTimer!==null)cancel(presenceTimer);\n      presenceTimer=later(()=>{presenceTimer=null;void presence();schedulePresence();},30000);\n    }\n    function nativeState() {\n      if(!active()||!hooks)return;\n      const value=hooks.getState();\n      if(value==='connected') {\n        if(connectionTimer!==null)cancel(connectionTimer);connectionTimer=null;\n        if(refreshTimer!==null)cancel(refreshTimer);refreshTimer=null;\n        if(retryTimer!==null)cancel(retryTimer);retryTimer=null;\n        if(!pending)publish({status:'ready'});\n      } else {\n        if(connectionTimer===null&&refreshTimer===null&&!pending&&state.status!=='recovery_failed'&&state.status!=='refresh_waiting')\n          connectionTimer=later(()=>{connectionTimer=null;if(active()&&hooks?.getState()!=='connected')void recover('native_disconnected');},8000);\n      }\n    }\n    function maybeRefresh() {\n      refreshTimer=null;\n      if(!active()||hooks?.getState()==='connected')return;\n      resetOldBudget();\n      // Unknown draft/upload/job state means keep the existing page intact.\n      let safe=false;try{safe=hooks?.canRefresh()===true;}catch{}\n      if(!safe||state.busy||!busyCheckedAt||now()-busyCheckedAt>45000||budget.refreshes>=1) {\n        publish({status:'refresh_waiting'});return;\n      }\n      budget.refreshes++;storeBudget();\n      if(!durable){publish({status:'refresh_waiting'});return;}\n      options.refresh();\n    }\n    async function recover(reason='manual') {\n      if(!active()||options.online?.()===false)return false;\n      if(pending)return pending;\n      resetOldBudget();\n      if(budget.attempts>=3){publish({status:'recovery_failed'});return false;}\n      if(connectionTimer!==null)cancel(connectionTimer);connectionTimer=null;\n      budget.attempts++;storeBudget();\n      const current=++generation;\n      publish({status:'recovering'});\n      pending=(async()=>{\n        const until=now()+120000;\n        try {\n          while(active()&&generation===current&&now()<until) {\n            const response=await request(options.openURL,{});\n            if(!active()||generation!==current)return false;\n            if(auth(response.status))return false;\n            if(response.status===202) {\n              const value=response.value;\n              if(!['starting','queued'].includes(value?.status))throw Error('INVALID_PENDING');\n              await new Promise(resolve=>later(resolve,value.status==='starting'?1000:5000));continue;\n            }\n            if(!response.ok)throw Error('OPEN_FAILED');\n            // Never replay the failed business request. Only replace transport.\n            hooks?.reconnect();\n            publish({status:hooks?.getState()==='connected'?'ready':'recovering'});\n            void presence();\n            if(refreshTimer!==null)cancel(refreshTimer);\n            refreshTimer=later(maybeRefresh,30000);\n            return true;\n          }\n          throw Error('OPEN_TIMEOUT');\n        } catch {\n          if(active()&&generation===current) {\n            publish({status:budget.attempts>=3?'recovery_failed':'recovering'});\n            if(budget.attempts<3)retryTimer=later(()=>{retryTimer=null;void recover('retry');},5000);\n          }\n          return false;\n        } finally {pending=null;}\n      })();\n      return pending;\n    }\n    function attachNative(value) {\n      unsubscribe?.();hooks=value;unsubscribe=hooks.subscribe(nativeState);nativeState();\n      if(state.status==='recovering')hooks.reconnect();\n      return()=>{if(hooks!==value)return;unsubscribe?.();unsubscribe=null;hooks=null;};\n    }\n    function start(){if(disposed)return;paused=false;void presence();schedulePresence();nativeState();}\n    function pause(){if(disposed||paused)return;void presence(false);paused=true;generation++;clearTimers();}\n    const api=Object.freeze({\n      getSnapshot:()=>state,\n      subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn);},\n      recover,attachNative,auth,start,pause,\n      busy:value=>{if(typeof value==='boolean'){busyCheckedAt=now();publish({busy:value});}},\n      hasNative:()=>!!hooks,\n      dispose(){pause();disposed=true;unsubscribe?.();listeners.clear();},\n    });\n    return api;\n  };\n})(globalThis);\n\n(function installWebSession(){\n  'use strict';\n  const original=window.fetch.bind(window),listeners=new Set();\n  const activityURL='/v1/personal-cloud/web/activity';\n  let mounted=false,activityPending=false,lastActivity=0,pollPending=false,pollTimer=null,requestsPending=0;\n  let snapshot=Object.freeze({status:'ready',busy:false}),panel=null;\n  const watchedDocuments=new Map(),watchedFrames=new Map();\n  const events=['pointerdown','keydown','input','wheel','touchstart'];\n  let tabStorage;try{tabStorage=sessionStorage;}catch{}\n  const recovery=globalThis.__MEGMEET_CREATE_CLOUD_RECOVERY__({\n    request:original,openURL:'/v1/personal-cloud/open',presenceURL:'/v1/personal-cloud/presence',\n    clientId:crypto.randomUUID(),storage:tabStorage,online:()=>navigator.onLine!==false,\n    refresh:()=>location.reload(),\n  });\n  globalThis.__MEGMEET_CLOUD_RECOVERY__=recovery;\n  function publish(value){snapshot=Object.freeze({...snapshot,...value});for(const fn of listeners){try{fn(snapshot);}catch{}}renderStatus();}\n  recovery.subscribe(publish);\n  function pathOf(input){try{const url=new URL(typeof input==='string'?input:input?.url||String(input),location.href);return url.origin===location.origin?url.pathname:'';}catch{return '';}}\n  function nativePath(path){return ['/api/','/plugins/','/assets/','/ext/','/__megmeet/','/v1/personal-cloud/'].some(prefix=>path.startsWith(prefix));}\n  function terminal(){return ['expired','blocked','signing_out'].includes(snapshot.status);}\n  function renderStatus(){\n    if(!mounted)return;\n    const messages={\n      recovering:'正在恢复连接，当前输入会保留…',\n      recovery_failed:'暂时未能恢复连接。当前输入已保留，请稍后重新连接。',\n      refresh_waiting:'连接仍未恢复。请先处理当前草稿、附件或任务，再刷新页面。',\n      expired:'登录已失效。请先保存当前输入，再重新登录。',\n      blocked:'当前账号暂时无法使用麦米，请联系管理员确认授权。',\n    };\n    const message=messages[snapshot.status];\n    if(!message){panel?.remove();panel=null;return;}\n    if(!panel){\n      panel=document.createElement('section');panel.id='megmeet-connection-status';panel.setAttribute('role','status');\n      panel.style.cssText='position:fixed;right:18px;bottom:18px;max-width:440px;z-index:2147483646;padding:12px 16px;border:1px solid #cad9cd;border-radius:12px;background:#f9faf6;color:#254337;font:14px/1.6 system-ui;box-shadow:0 4px 20px #173a5018';\n      document.body.append(panel);\n    }\n    panel.replaceChildren();const text=document.createElement('span');text.textContent=message;panel.append(text);\n    if(snapshot.status==='expired'){\n      const button=document.createElement('a');button.textContent='重新登录';button.href='/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F';button.style.marginLeft='12px';panel.append(button);\n    }else if(['recovery_failed','refresh_waiting'].includes(snapshot.status)){\n      const button=document.createElement('button');button.textContent='重新连接';button.style.marginLeft='12px';button.onclick=()=>void recovery.recover('manual');panel.append(button);\n    }\n  }\n  globalThis.__MEGMEET_WEB_SESSION__=Object.freeze({\n    getSnapshot:()=>snapshot,subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn);},\n    resume:()=>{if(terminal())renderStatus();else void recovery.recover('manual');},\n    safeToRefresh:()=>!terminal()&&requestsPending===0,\n    logout:async()=>{\n      if(terminal())return;const previous=snapshot;recovery.pause();publish({status:'signing_out'});\n      try{const response=await original('/v1/iam/logout',{method:'POST',credentials:'same-origin'});if(!response.ok)throw Error('LOGOUT_FAILED');location.replace('/maimi/');}\n      catch(error){publish(previous);recovery.start();throw error;}\n    },\n  });\n  async function statusResponse(response){\n    if(recovery.auth(response.status))return;\n    if(response.status===409){void recovery.recover('host_missing');return;}\n    if(!response.ok||terminal())return;\n    const value=await response.json();\n    if(value.status==='released')void recovery.recover('host_missing');\n    if(value.status==='ready')recovery.busy(value.busy);\n  }\n  async function markActivity(){\n    if(terminal()||activityPending||Date.now()-lastActivity<15000)return;\n    lastActivity=Date.now();activityPending=true;\n    try{await statusResponse(await original(activityURL,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',credentials:'same-origin',cache:'no-store'}));}catch{}finally{activityPending=false;}\n  }\n  const userActivity=event=>{if(event.isTrusted===true)void markActivity();};\n  function watchDocument(doc){\n    if(!doc||watchedDocuments.has(doc))return;\n    for(const event of events)doc.addEventListener(event,userActivity,{capture:true,passive:true});\n    function scan(){for(const frame of doc.querySelectorAll?.('iframe')||[]){if(watchedFrames.has(frame))continue;const loaded=()=>{try{watchDocument(frame.contentDocument);}catch{}};watchedFrames.set(frame,loaded);frame.addEventListener('load',loaded);loaded();}}\n    const observer=typeof MutationObserver==='function'?new MutationObserver(scan):null;\n    watchedDocuments.set(doc,()=>{observer?.disconnect();for(const event of events)doc.removeEventListener(event,userActivity,true);});\n    if(doc.documentElement)observer?.observe(doc.documentElement,{childList:true,subtree:true});scan();\n  }\n  async function checkStatus(){\n    if(terminal()||pollPending)return;pollPending=true;\n    try{await statusResponse(await original(activityURL,{credentials:'same-origin',cache:'no-store'}));}catch{}finally{pollPending=false;}\n  }\n  window.fetch=async(...args)=>{\n    const path=pathOf(args[0]),native=nativePath(path);\n    if(native&&terminal())return new Response(JSON.stringify({detail:snapshot.status==='blocked'?'account_unavailable':'authentication_required'}),{status:snapshot.status==='blocked'?403:401,headers:{'Content-Type':'application/json'}});\n    if(native)requestsPending++;\n    try{\n      const response=await original(...args);\n      // A project-specific 403 is not proof that the employee account is disabled.\n      if(native&&response.status===401)recovery.auth(response.status);\n      if(native&&response.status===409){try{if((await response.clone().json())?.detail==='personal_cloud_open_required')void recovery.recover('host_missing');}catch{}}\n      return response;\n    }finally{if(native)requestsPending--;}\n  };\n  function mount(){if(mounted)return;mounted=true;watchDocument(document);recovery.start();renderStatus();pollTimer=setInterval(checkStatus,30000);}\n  window.addEventListener('pagehide',()=>{recovery.pause();if(pollTimer!==null)clearInterval(pollTimer);pollTimer=null;});\n  window.addEventListener('pageshow',()=>{if(mounted&&!terminal()){recovery.start();if(pollTimer===null)pollTimer=setInterval(checkStatus,30000);}});\n  window.addEventListener('online',()=>{if(!terminal())void recovery.recover('online');});\n  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',mount,{once:true}):mount();\n})();\n",media_type='text/javascript',headers=HEADERS)

@router.get('/maimi/web/image.js')
async def image_script():
    # DSH alpha.2 images otherwise travel inside base64 prompt JSON, bypassing
    # file-upload inspection. Adapt only its pinned unary prompt wire envelope;
    # native image validation, admission, storage, and draft UI stay in DSH.
    return Response('''(()=>{
'use strict';
if(globalThis.__MEGMEET_IMAGE_UPLOAD_INSTALLED__)return;
globalThis.__MEGMEET_IMAGE_UPLOAD_INSTALLED__=true;
// Installed after session.js: this is the existing r15 idle/auth guard.
const guardedFetch=window.fetch.bind(window),imagePath='/v1/personal-cloud/web/image';
const imageLimit=8*1024*1024,totalLimit=16*1024*1024,promptLimit=32*1024*1024;
const responseLimit=Math.ceil(imageLimit/3)*4+16384;
const mediaTypes=new Set(['image/png','image/jpeg','image/webp','image/gif']);
function aborted(signal){if(signal?.aborted)throw signal.reason??new DOMException('The operation was aborted.','AbortError');}
class ImageUploadProblem extends Error{}
function problem(message){return new ImageUploadProblem(message);}
function decode(data){
  if(typeof data!=='string'||data.length===0||data.length>Math.ceil(imageLimit/3)*4||data.length%4!==0||!/^[A-Za-z0-9+/]*={0,2}$/.test(data))throw problem('图片内容无效或超过8 MiB，请重新选择图片。');
  let text;try{text=atob(data);}catch{throw problem('图片内容无效，请重新选择图片。');}
  if(text.length>imageLimit||btoa(text)!==data)throw problem('图片内容无效或超过8 MiB，请重新选择图片。');
  const bytes=new Uint8Array(text.length);for(let i=0;i<text.length;i++)bytes[i]=text.charCodeAt(i);
  return bytes;
}
function mediaOf(bytes){
  const at=(offset,values)=>values.every((value,index)=>bytes[offset+index]===value);
  if(at(0,[137,80,78,71,13,10,26,10]))return 'image/png';
  if(at(0,[255,216,255]))return 'image/jpeg';
  if(at(0,[71,73,70,56])&&(bytes[4]===55||bytes[4]===57)&&bytes[5]===97)return 'image/gif';
  if(bytes.length>=12&&at(0,[82,73,70,70])&&at(8,[87,69,66,80]))return 'image/webp';
  return '';
}
async function boundedJson(response,signal){
  const reader=response.body?.getReader();if(!reader)throw problem('图片处理服务返回异常，请稍后重试。');
  const chunks=[];let bytes=0,done=false;
  try{while(true){aborted(signal);const item=await reader.read();aborted(signal);if(item.done){done=true;break;}bytes+=item.value.byteLength;if(bytes>responseLimit)throw problem('图片处理服务返回的内容过大。');chunks.push(item.value);}}
  finally{if(!done){try{await reader.cancel();}catch{}}try{reader.releaseLock();}catch{}}
  try{return JSON.parse(await new Blob(chunks).text());}catch{throw problem('图片处理服务返回异常，请稍后重试。');}
}
function serviceError(status,detail){
  if(status===401)return '登录已失效，请重新登录后发送图片。';
  if(detail==='personal_cloud_open_required')return '云端工作区已释放，请重新进入后发送图片。';
  const messages={personal_cloud_image_invalid:'无法识别图片，请重新选择完整图片。',personal_cloud_image_type_unsupported:'请使用PNG、JPEG、GIF或WebP图片。',personal_cloud_image_too_large:'图片超过8 MiB，请缩小后重新上传。',personal_cloud_image_still_encrypted:'图片仍是加密内容，安全网关尚未完成解密，请联系管理员。',personal_cloud_image_busy:'图片处理繁忙，请稍后重试。'};
  return messages[detail]??'图片上传处理失败，请稍后重试。';
}
function errorResponse(envelope,error){
  return new Response(JSON.stringify({type:'server-response',rpcId:envelope.rpcId,result:{ok:false,error:{code:'session/image-upload-failed',message:error.message,details:{reason:'IMAGE_GATEWAY_UPLOAD_FAILED'}}}}),{status:200,headers:{'Content-Type':'application/json','Cache-Control':'no-store'}});
}
window.fetch=async(input,init)=>{
  const options=init??{};let url;
  try{url=new URL(typeof input?.url==='string'?input.url:input,location.href);}catch{return guardedFetch(input,init);}
  const endpoint=url.pathname==='/api/session/prompt'?'session/prompt':url.pathname==='/api/subagents/prompt'?'subagents/prompt':'';
  if(!endpoint||url.origin!==location.origin||(options.method??'GET').toUpperCase()!=='POST'||typeof options.body!=='string'||new Headers(options.headers).get('content-type')?.split(';',1)[0].trim().toLowerCase()!=='application/json')return guardedFetch(input,init);
  let envelope;try{envelope=JSON.parse(options.body);}catch{return guardedFetch(input,init);}
  const content=envelope?.payload?.args?.request?.content;
  if(envelope?.type!=='client-request'||typeof envelope.rpcId!=='string'||envelope.method!==endpoint||!Array.isArray(content)||!content.some(part=>part?.type==='image'))return guardedFetch(input,init);
  try{
    aborted(options.signal);
    if(url.protocol!=='https:'||(url.port!==''&&url.port!=='443')||url.username||url.password||url.hash)throw problem('图片上传必须通过本站HTTPS 443入口。');
    if(options.body.length>promptLimit||new Blob([options.body]).size>promptLimit)throw problem('本次消息内容过大，请减少图片后重试。');
    const images=content.filter(part=>part?.type==='image');if(images.length>8)throw problem('一次最多发送8张图片，请分批发送。');
    let originalTotal=0,plainTotal=0;const replacements=new Map();
    // Validate every source before sending the first image. Ciphertext headers
    // are deliberately allowed here: only the gateway response must be an image.
    const sources=images.map(part=>{
      if(!mediaTypes.has(part.mediaType)||typeof part.data!=='string')throw problem('图片内容无效，请重新选择图片。');
      const bytes=decode(part.data);originalTotal+=bytes.byteLength;
      if(originalTotal>totalLimit)throw problem('本次图片总大小超过16 MiB，请分批发送。');
      if(part.name!==undefined&&(typeof part.name!=='string'||part.name.length>255))throw problem('图片文件名无效，请重新选择图片。');
      const name=part.name||'image.'+({'image/png':'png','image/jpeg':'jpg','image/gif':'gif','image/webp':'webp'}[part.mediaType]);
      return {part,bytes,name};
    });
    for(const source of sources){
      aborted(options.signal);
      const target=new URL(imagePath,location.origin);target.searchParams.set('filename',source.name);target.searchParams.set('size',String(source.bytes.byteLength));
      // Re-enter the current chain for a non-prompt path; never bypass the
      // r15 guard or use a saved native fetch for image preprocessing.
      const response=await window.fetch(target,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:new Blob([source.bytes],{type:'application/octet-stream'}),signal:options.signal,credentials:'same-origin',mode:'same-origin',redirect:'error',cache:'no-store'});
      const value=await boundedJson(response,options.signal);if(!response.ok)throw problem(serviceError(response.status,value?.detail));
      const bytes=decode(value?.data);
      if(!mediaTypes.has(value?.mediaType)||mediaOf(bytes)!==value.mediaType||typeof value?.name!=='string'||value.name.length>255)throw problem('图片处理服务未返回有效图片，请重新上传。');
      plainTotal+=bytes.byteLength;if(plainTotal>totalLimit)throw problem('解密后的图片总大小超过16 MiB，请分批发送。');
      replacements.set(source.part,{...source.part,mediaType:value.mediaType,data:value.data,name:value.name});
    }
    envelope.payload.args.request.content=content.map(part=>replacements.get(part)??part);
    const body=JSON.stringify(envelope);if(new Blob([body]).size>promptLimit)throw problem('本次消息内容过大，请减少图片后重试。');
    aborted(options.signal);
    return guardedFetch(input,{...options,body});
  }catch(error){
    aborted(options.signal);
    // A fixed message prevents arbitrary transport/server text from exposing
    // credentials or file contents; our own messages contain no user data.
    const known=error instanceof ImageUploadProblem;
    return errorResponse(envelope,known?error:problem('图片上传处理失败，请稍后重试。'));
  }
};
})();''',media_type='text/javascript',headers=HEADERS)

@router.websocket('/api/remote.mux')
async def websocket(socket:WebSocket):
    await cloud.native_ws(socket)

@router.api_route('/api/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD'])
async def api(request:Request,path:str):
    if request.method=='POST' and path=='officeToPdf/render':return await render_preview(request)
    return await cloud.native_http(request,'api/'+path)
@router.api_route('/plugins/{path:path}',methods=['GET','HEAD'])
async def plugins(request:Request,path:str):return await cloud.native_http(request,'plugins/'+path)
@router.api_route('/assets/{path:path}',methods=['GET','HEAD'])
async def assets(request:Request,path:str):return await cloud.native_http(request,'assets/'+path)
@router.api_route('/ext/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD'])
async def extensions(request:Request,path:str):return await cloud.native_http(request,'ext/'+path)
@router.get('/manifest.webmanifest')
async def manifest(request:Request):return await cloud.native_http(request,'manifest.webmanifest')
@router.get('/favicon.svg')
async def favicon(request:Request):return await cloud.native_http(request,'favicon.svg')

@router.get('/__megmeet/desktop/workspace')
async def workspace(request:Request):
    await cloud.actor_for(request)
    return JSONResponse(WEB_STATE,headers=HEADERS)

@router.get('/__megmeet/desktop/session')
async def desktop_session(request:Request):
    actor=await cloud.actor_for(request)
    _,expires=await cloud.issue_lease(request,actor)
    return JSONResponse({'user':actor.public(),'mode':'cloud','client':'web','expiresAt':int(expires*1000)},headers=HEADERS)

@router.post('/__megmeet/desktop/local/open')
async def no_local(request:Request):
    await cloud.actor_for(request)
    raise iam.IAMFailure(403,'web_personal_cloud_only')

@router.post('/__megmeet/desktop/cloud/open')
async def open_cloud(request:Request):
    response=await cloud.open_workspace(request)
    return response if response.status_code!=200 else JSONResponse(WEB_STATE,headers=HEADERS)

@router.api_route('/__megmeet/desktop/models{suffix:path}',methods=['GET','POST'])
async def models(request:Request,suffix:str):
    if suffix=='/refresh' and request.method=='POST':return await cloud.refresh_models(request)
    if suffix=='' and request.method=='GET':
        actor=await cloud.actor_for(request)
        value=await cloud.model.get_broker().models(actor)
        return JSONResponse({'models':value['data'],'defaultModel':value['default_model'],
            'preferredAvailable':value['default_model']==cloud.model.DEFAULT_MODEL,'notice':''},headers=HEADERS)
    raise iam.IAMFailure(404,'not_found')

@router.api_route('/__megmeet/tools/{path:path}',methods=['GET','POST'])
async def tools(request:Request,path:str):
    methods={'catalog':'GET','state':'GET','selection':'POST','confirm':'POST','stop':'POST',
             'invoke':'POST','cancel-job':'POST','form-ticket':'POST','secure-form':'POST'}
    if path not in methods:raise iam.IAMFailure(404,'not_found')
    if request.method!=methods[path]:raise iam.IAMFailure(405,'method_not_allowed')
    return await cloud.native_http(request,'__megmeet/tools/'+path)

def enterprise_target(method,path,query):
    if '%' in path or '#' in path:raise iam.IAMFailure(404,'not_found')
    if method=='GET' and path=='plugins' and not query:return '/v1/plugins/catalog'
    if method=='GET' and path=='personal-cloud/history/trash' and re.fullmatch(r'(?:page=\d{1,5})?',query):return '/v1/'+path
    if method=='POST' and path in {'personal-cloud/history/restore','plugins/uploads','plugins/tasks'} and not query:return '/v1/'+path
    if method=='GET' and path=='plugins/results' and re.fullmatch(r'native_session_id=(?:session-)?[a-fA-F0-9-]{36}&location=cloud',query):return '/v1/'+path
    if path.startswith('connectors/') and re.fullmatch(r'connectors/[a-zA-Z0-9_/-]{1,160}',path):return '/v1/'+path
    raise iam.IAMFailure(404,'not_found')

@router.api_route('/__megmeet/enterprise/{path:path}',methods=['GET','POST'])
async def enterprise(request:Request,path:str):
    await cloud.actor_for(request)
    target=enterprise_target(request.method,path,request.url.query)
    body=bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body)>512*1024:raise iam.IAMFailure(413,'enterprise_request_too_large')
    # Dispatch to the actual registered Gateway endpoint; keep its Pydantic,
    # employee ACL, request origin and task/attachment authorization checks.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app),base_url=iam.IAMConfig.from_env().public_origin) as client:
        response=await client.request(request.method,target+('?' + request.url.query if request.url.query else ''),
            headers={k:v for k,v in request.headers.items() if k.lower() in {'cookie','origin','content-type'}},content=bytes(body))
    return Response(response.content,status_code=response.status_code,media_type=response.headers.get('content-type'),headers=HEADERS)
