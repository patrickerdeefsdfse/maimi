/* r47: own authenticated Host recovery, independent of translated UI text. */
(function installCloudRecovery(target) {
  'use strict';
  if (target.__MEGMEET_CREATE_CLOUD_RECOVERY__) return;
  target.__MEGMEET_CREATE_CLOUD_RECOVERY__ = function createCloudRecovery(options) {
    const now = options.now || Date.now;
    const later = options.setTimeout || target.setTimeout.bind(target);
    const cancel = options.clearTimeout || target.clearTimeout.bind(target);
    const key = 'megmeet.cloud.recovery.r47';
    const windowMs = 300000, listeners = new Set();
    let state = Object.freeze({status:'ready', busy:false}), disposed = false, paused = false;
    let pending = null, hooks = null, unsubscribe = null;
    let connectionTimer = null, retryTimer = null, refreshTimer = null, presenceTimer = null;
    let presencePending = false, presenceQueued = false, generation = 0, busyCheckedAt = 0;
    let replaceTransport = false, sendCheck = null;
    const foreground=()=>options.foreground?.()!==false;
    const nativeReady=()=>!!hooks && hooks.getState()==='connected' &&
      (!hooks.getGeneration || Number.isSafeInteger(hooks.getGeneration()));
    let queueRequest=null, queueDeadline=0, queueStopped=false;
    let budget = {started:now(), attempts:0, refreshes:0}, durable = false;
    try {
      const saved = JSON.parse(options.storage?.getItem(key) || 'null');
      if (saved && Number.isFinite(saved.started) && Number.isInteger(saved.attempts)
          && Number.isInteger(saved.refreshes) && saved.attempts >= 0 && saved.refreshes >= 0
          && now() >= saved.started && now()-saved.started < windowMs) budget = saved;
    } catch {}
    function publish(value) {
      state = Object.freeze({...state,...value});
      for (const listener of listeners) { try { listener(state); } catch {} }
    }
    function storeBudget() {
      durable = false;
      try { const value=JSON.stringify(budget); options.storage?.setItem(key,value); durable=options.storage?.getItem(key)===value; } catch {}
    }
    function resetOldBudget() {
      if (now() < budget.started || now()-budget.started >= windowMs) budget={started:now(),attempts:0,refreshes:0};
    }
    function terminal() { return ['expired','blocked','signing_out'].includes(state.status); }
    function clearTimers() {
      for (const timer of [connectionTimer,retryTimer,refreshTimer,presenceTimer]) if(timer!==null)cancel(timer);
      connectionTimer=retryTimer=refreshTimer=presenceTimer=null;
    }
    function auth(status, iamConfirmed=false) {
      if (![401,403].includes(status) || disposed) return false;
      if(status===401&&!iamConfirmed){
        // The worker, connector and IAM credentials have independent lifetimes.
        target.dispatchEvent(new Event('maimi:verify-web-session'));
        return true;
      }
      generation++; clearTimers();
      publish({status:status===401?'expired':'blocked',busy:false});
      options.onAuth?.(status);
      return true;
    }
    function active() {return !disposed&&!paused&&!terminal();}
    async function request(path, body, keepalive=false) {
      if(path===options.presenceURL)body={...body,foreground:body.present===true&&foreground()};
      const controller = new AbortController();
      let timeout;
      const expired=new Promise((resolve,reject)=>{timeout=later(()=>{controller.abort();reject(Error('CONTROL_TIMEOUT'));},15000);});
      try {return await Promise.race([expired,(async()=>{
        const response=await options.request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),credentials:'same-origin',cache:'no-store',signal:controller.signal,...(keepalive?{keepalive:true}:{})});
        // Keep the timeout active through body parsing; headers alone are not completion.
        const value=(response.ok||response.status===409||response.status===503)&&response.status!==204?await response.json():null;
        return {status:response.status,ok:response.ok,value};
      })()]);}
      finally {cancel(timeout);}
    }
    async function presence(present=true, changedVisibility=false) {
      if (disposed||terminal()||(present&&paused)) return;
      if(present&&presencePending){if(changedVisibility)presenceQueued=true;return;}
      if(present)presencePending=true;
      const current=generation;
      try {
        const response=await request(options.presenceURL,{client_id:options.clientId,present},!present);
        if (disposed||paused||!present||current!==generation) return;
        if(auth(response.status))return;
        if(response.status===409&&response.value?.detail==='personal_cloud_open_required'&&!queueStopped&&!pending){replaceTransport=true;queueStopped=true;publish({status:'idle_released'});}
        else if(response.ok&&typeof response.value?.busy==='boolean'){busyCheckedAt=now();publish({busy:response.value.busy});}
      } catch {} finally {
        if(present){presencePending=false;if(presenceQueued){presenceQueued=false;void presence();}}
      }
    }
    function schedulePresence() {
      if(!active())return;
      if(presenceTimer!==null)cancel(presenceTimer);
      presenceTimer=later(()=>{presenceTimer=null;void presence();schedulePresence();},30000);
    }
    function nativeState() {
      if(!active()||!hooks||queueStopped)return;
      const value=hooks.getState();
      if(nativeReady()&&!replaceTransport) {
        target.__MEGMEET_ENTRY_TIMING__?.mark('transport_ready');
        if(connectionTimer!==null)cancel(connectionTimer);connectionTimer=null;
        if(refreshTimer!==null)cancel(refreshTimer);refreshTimer=null;
        if(retryTimer!==null)cancel(retryTimer);retryTimer=null;
        if(!pending)publish({status:'ready'});
      } else {
        if(foreground()&&value!=='connected'&&connectionTimer===null&&refreshTimer===null&&!pending&&state.status!=='recovery_failed'&&state.status!=='refresh_waiting')
          connectionTimer=later(()=>{connectionTimer=null;if(active()&&hooks?.getState()!=='connected')void recover('native_disconnected');},8000);
      }
    }
    function maybeRefresh() {
      refreshTimer=null;
      if(!active()||queueStopped)return;
      if(!hooks){publish({status:'transport_unavailable'});return;}
      if(nativeReady()&&!replaceTransport){publish({status:'ready'});return;}
      // The native loop owns transport retry. Never infer disposable drafts or reload.
      publish({status:'refresh_waiting'});
    }
    async function recover(reason='manual') {
      if(!active()||!foreground()||options.online?.()===false)return false;
      if(pending)return pending;
      if(reason==='manual'){queueStopped=false;queueRequest=null;queueDeadline=0;budget={started:now(),attempts:0,refreshes:0};}
      else if(reason==='user_activity'&&state.status==='idle_released'){queueStopped=false;}
      else if(queueStopped)return false;
      resetOldBudget();
      if(budget.attempts>=3){publish({status:'recovery_failed'});return false;}
      if(connectionTimer!==null)cancel(connectionTimer);connectionTimer=null;
      budget.attempts++;storeBudget();
      const current=++generation;
      publish({status:'recovering'});
      const attempt=(async()=>{
        const until=queueDeadline||now()+300000;
        try {
          if(reason!=='manual'&&reason!=='user_activity'&&!queueRequest){
            const probe=await request(options.presenceURL,{client_id:options.clientId,present:true});
            if(!active()||generation!==current)return false;
            if(auth(probe.status))return false;
            if(probe.status===409&&probe.value?.detail==='personal_cloud_open_required'){replaceTransport=true;queueStopped=true;publish({status:'idle_released'});return false;}
            if(!probe.ok)throw Error('HOST_STATE_UNKNOWN');
          }
          while(active()&&generation===current&&now()<until) {
            if(!foreground())return false;
            if(queueDeadline&&now()>=queueDeadline){queueStopped=true;publish({status:'queue_expired'});return false;}
            const response=await request(options.openURL,queueRequest?{queue_request_id:queueRequest}:{});
            if(!active()||generation!==current)return false;
            if(auth(response.status))return false;
            if((response.status===409&&['personal_cloud_queue_expired','personal_cloud_queue_cancelled'].includes(response.value?.detail))||response.status===429){queueStopped=true;publish({status:'queue_expired'});return false;}
            if(response.status===202) {
              const value=response.value;
              if(value?.status==='queued'){
                if(!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.request_id)||!Number.isFinite(value.expires_at)||!Number.isInteger(value.position)||value.position<1||(queueRequest&&queueRequest!==value.request_id))throw Error('INVALID_QUEUE');
                queueRequest=value.request_id;queueDeadline=Math.min(until,value.expires_at*1000);
                publish({status:'queued',queuePosition:value.position});
              }
              if(!['starting','queued'].includes(value?.status))throw Error('INVALID_PENDING');
              await new Promise(resolve=>later(resolve,value.status==='starting'?1000:5000));continue;
            }
            if(!response.ok)throw Error('OPEN_FAILED');
            queueRequest=null;queueDeadline=0;
            // Host readiness is separate from the native event generation.
            if(!hooks){publish({status:'transport_unavailable'});return false;}
            if(nativeReady()&&!replaceTransport){publish({status:'ready'});void presence();return true;}
            // Never replay the failed business request. Only replace transport.
            replaceTransport=false;
            hooks.reconnect();
            publish({status:nativeReady()?'ready':'recovering'});
            void presence();
            if(refreshTimer!==null)cancel(refreshTimer);
            refreshTimer=later(maybeRefresh,30000);
            return true;
          }
          if(active()&&generation===current){queueStopped=true;publish({status:'queue_expired'});}
          return false;
        } catch {
          if(active()&&generation===current&&!queueStopped) {
            if(queueRequest){queueStopped=true;publish({status:'queue_expired'});return false;}
            publish({status:budget.attempts>=3?'recovery_failed':'recovering'});
            if(budget.attempts<3)retryTimer=later(()=>{retryTimer=null;void recover('retry');},5000);
          }
          return false;
        }
      })();
      pending=attempt;
      try{return await attempt;}finally{if(pending===attempt)pending=null;}
    }
    function attachNative(value) {
      unsubscribe?.();hooks=value;unsubscribe=hooks.subscribe(nativeState);nativeState();
      return()=>{if(hooks!==value)return;unsubscribe?.();unsubscribe=null;hooks=null;};
    }
    function start(){if(disposed)return;paused=false;void presence();schedulePresence();nativeState();}
    function pause(){
      if(disposed||paused)return;
      void presence(false);paused=true;generation++;pending=null;sendCheck=null;clearTimers();
      publish({});
    }
    const sendReady=()=>active() && foreground() && !queueStopped && !replaceTransport && nativeReady() && state.status==='ready';
    function released(userActivity){
      if(!active()||pending||queueStopped&&state.status!=='idle_released')return;
      replaceTransport=true;queueStopped=true;publish({status:'idle_released'});
      if(userActivity&&foreground())void recover('user_activity');
    }
    async function verifySendHost(){
      if(!active()||!foreground()||options.online?.()===false)return false;
      if(pending){const ok=await pending;if(ok||state.status!=='idle_released')return ok;return recover('user_activity');}
      const sample=generation;
      const probe=await request(options.presenceURL,{client_id:options.clientId,present:true});
      if(!active()||!foreground()||auth(probe.status))return false;
      if(sample!==generation)return pending||sendReady();
      const released=probe.status===409&&probe.value?.detail==='personal_cloud_open_required';
      if(!probe.ok&&!released)return false;
      if(queueStopped&&state.status!=='idle_released')return false;
      if(released){replaceTransport=true;queueStopped=true;publish({status:'idle_released'});}
      if(sendReady())return true;
      return recover('user_activity');
    }
    function beforeSend(signal){
      // Only the shared readiness check can continue after caller cancellation.
      // The original native request remains with its caller and is sent at most once.
      if(signal?.aborted || !active() || !foreground())return Promise.resolve(false);
      if(!sendCheck){
        // A fresh submission can rejoin after cancellation or expiry. An older submission stays cancelled.
        const newQueue=queueStopped&&['queue_cancelled','queue_expired','queue_cancel_unconfirmed'].includes(state.status);
        const check=(newQueue?recover('manual'):verifySendHost()).catch(()=>false);
        sendCheck=check;
        void check.finally(()=>{if(sendCheck===check)sendCheck=null;});
      }
      return new Promise(resolve=>{
        let allowed=false,finished=false;
        function finish(ok){
          if(finished)return;finished=true;cancel(timeout);listeners.delete(changed);
          signal?.removeEventListener('abort',aborted);resolve(ok);
        }
        function changed(){if(!active()||!foreground()||queueStopped&&state.status!=='idle_released')finish(false);else if(allowed&&sendReady())finish(true);}
        const aborted=()=>finish(false);
        const timeout=later(aborted,45000);
        listeners.add(changed);signal?.addEventListener('abort',aborted,{once:true});
        sendCheck.then(ok=>{if(!ok){finish(false);return;}allowed=true;changed();});
      });
    }
    const api=Object.freeze({
      getSnapshot:()=>state,
      subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn);},
      recover,attachNative,auth,start,pause,beforeSend,released,
      visibility:()=>{publish({});void presence(true,true);if(foreground())nativeState();},
      cancelQueue:async()=>{
        if(!queueRequest||queueStopped)return;
        const request_id=queueRequest;
        queueStopped=true;generation++;pending=null;sendCheck=null;clearTimers();publish({status:'queue_cancelling'});
        let confirmed=false;
        try{confirmed=(await request('/v1/personal-cloud/queue/cancel',{request_id})).ok;}catch{}
        if(queueRequest===request_id&&state.status==='queue_cancelling'){
          publish({status:confirmed?'queue_cancelled':'queue_cancel_unconfirmed'});
        }
      },
      busy:value=>{if(typeof value==='boolean'){busyCheckedAt=now();publish({busy:value});}},
      hasNative:()=>!!hooks,
      adapterUnavailable:()=>{if(active()&&!hooks)publish({status:'transport_unavailable'});},
      dispose(){pause();disposed=true;unsubscribe?.();listeners.clear();},
    });
    return api;
  };
})(globalThis);

(function installWebSession(){
  'use strict';
  const original=window.fetch.bind(window),listeners=new Set();
  const activityURL='/v1/personal-cloud/web/activity';
  let mounted=false,activityPending=false,lastActivity=0,pollPending=false,pollTimer=null,requestsPending=0;
  let snapshot=Object.freeze({status:'ready',busy:false}),panel=null;
  const watchedDocuments=new Map(),watchedFrames=new Map();
  const events=['pointerdown','keydown','input','wheel','touchstart'];
  let tabStorage;try{tabStorage=sessionStorage;}catch{}
  const recovery=globalThis.__MEGMEET_CREATE_CLOUD_RECOVERY__({
    request:original,openURL:'/v1/personal-cloud/open',presenceURL:'/v1/personal-cloud/presence',
    clientId:crypto.randomUUID(),storage:tabStorage,online:()=>navigator.onLine!==false,foreground:()=>!document.hidden,
  });
  globalThis.__MEGMEET_CLOUD_RECOVERY__=recovery;
  window.addEventListener('maimi:web-session-ended',()=>recovery.auth(401,true));
  function publish(value){snapshot=Object.freeze({...snapshot,...value});for(const fn of listeners){try{fn(snapshot);}catch{}}renderStatus();}
  recovery.subscribe(publish);
  function pathOf(input){try{const url=new URL(typeof input==='string'?input:input?.url||String(input),location.href);return url.origin===location.origin?url.pathname:'';}catch{return '';}}
  function nativePath(path){return ['/api/','/plugins/','/assets/','/ext/','/__megmeet/','/v1/personal-cloud/'].some(prefix=>path.startsWith(prefix));}
  function terminal(){return ['expired','blocked','signing_out'].includes(snapshot.status);}
  function renderStatus(){
    if(!mounted)return;
    const messages={
      queued:'正在等待空闲工作区（第 '+(snapshot.queuePosition||1)+' 位）。当前输入会保留，最多等待 5 分钟。',
      queue_cancel_unconfirmed:'取消结果尚未确认，已停止轮询。等待请求会到期。',
      blocked:'当前账号暂时无法使用麦米，请联系管理员确认授权。',
    };
    const message=messages[snapshot.status];
    if(!message){panel?.remove();panel=null;return;}
    if(!panel){
      panel=document.createElement('section');panel.id='megmeet-connection-status';panel.setAttribute('role','status');
      panel.style.cssText='position:fixed;top:12px;left:50%;transform:translateX(-50%);width:max-content;max-width:min(90vw,580px);z-index:1000;padding:8px 14px;border:1px solid #cad9cd;border-radius:8px;background:#f9faf6;color:#254337;font:14px/1.5 system-ui';
      document.body.append(panel);
    }
    panel.replaceChildren();const text=document.createElement('span');text.textContent=message;panel.append(text);
    if(snapshot.status==='queued'){
      const button=document.createElement('button');button.textContent='取消排队';button.onclick=()=>void recovery.cancelQueue();panel.append(button);
    }
  }
  globalThis.__MEGMEET_WEB_SESSION__=Object.freeze({
    getSnapshot:()=>snapshot,subscribe:fn=>{listeners.add(fn);return()=>listeners.delete(fn);},
    resume:()=>{if(terminal())renderStatus();else void recovery.recover('manual');},
    safeToRefresh:()=>!terminal()&&requestsPending===0,
    logout:async()=>{
      if(terminal())return;const previous=snapshot;recovery.pause();publish({status:'signing_out'});
      try{const response=await original('/v1/iam/logout',{method:'POST',credentials:'same-origin'});if(!response.ok)throw Error('LOGOUT_FAILED');location.replace('/maimi/');}
      catch(error){publish(previous);recovery.start();throw error;}
    },
  });
  async function statusResponse(response, userActivity=false){
    if(recovery.auth(response.status))return;
    if(response.status===409){let failure;try{failure=await response.json();}catch{}if(failure?.detail==='personal_cloud_open_required')recovery.released(userActivity);return;}
    if(!response.ok||terminal())return;
    const value=await response.json();
    if(value.status==='released')recovery.released(userActivity);
    if(value.status==='ready')recovery.busy(value.busy);
  }
  async function markActivity(){
    if(!document.hidden && snapshot.status==='idle_released')void recovery.recover('user_activity');
    if(terminal()||activityPending||Date.now()-lastActivity<15000)return;
    lastActivity=Date.now();activityPending=true;
    try{await statusResponse(await original(activityURL,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',credentials:'same-origin',cache:'no-store'}),true);}catch{}finally{activityPending=false;}
  }
  const userActivity=event=>{if(event.isTrusted===true)void markActivity();};
  function watchDocument(doc){
    if(!doc||watchedDocuments.has(doc))return;
    for(const event of events)doc.addEventListener(event,userActivity,{capture:true,passive:true});
    function scan(){for(const frame of doc.querySelectorAll?.('iframe')||[]){if(watchedFrames.has(frame))continue;const loaded=()=>{try{watchDocument(frame.contentDocument);}catch{}};watchedFrames.set(frame,loaded);frame.addEventListener('load',loaded);loaded();}}
    const observer=typeof MutationObserver==='function'?new MutationObserver(scan):null;
    watchedDocuments.set(doc,()=>{observer?.disconnect();for(const event of events)doc.removeEventListener(event,userActivity,true);});
    if(doc.documentElement)observer?.observe(doc.documentElement,{childList:true,subtree:true});scan();
  }
  async function checkStatus(){
    if(terminal()||pollPending)return;pollPending=true;
    try{await statusResponse(await original(activityURL,{credentials:'same-origin',cache:'no-store'}));}catch{}finally{pollPending=false;}
  }
  window.fetch=async(...args)=>{
    const path=pathOf(args[0]),native=nativePath(path);
    if(native&&terminal())return new Response(JSON.stringify({detail:snapshot.status==='blocked'?'account_unavailable':'authentication_required'}),{status:snapshot.status==='blocked'?403:401,headers:{'Content-Type':'application/json'}});
    if(native)requestsPending++;
    try{
      const method=args[1]?.method||args[0]?.method||'GET';
      if(method.toUpperCase()==='POST'&&/^(?:\/v1\/personal-cloud\/native)?\/api\/session\/(?:create|prompt)$/.test(path)){
        const signal=args[1]?.signal||args[0]?.signal;
        if(!await recovery.beforeSend(signal)){
          signal?.throwIfAborted();
          throw Error('工作区连接尚未就绪，本次请求尚未发送。请保留当前输入，连接恢复后再试。');
        }
        signal?.throwIfAborted();
      }
      const response=await original(...args);
      // A project-specific 403 is not proof that the employee account is disabled.
      if(native&&response.status===401)recovery.auth(response.status);
      if(native&&response.status===409){try{if((await response.clone().json())?.detail==='personal_cloud_open_required')recovery.released(false);}catch{}}
      return response;
    }finally{if(native)requestsPending--;}
  };
  function mount(){if(mounted)return;mounted=true;watchDocument(document);recovery.start();renderStatus();pollTimer=setInterval(checkStatus,30000);}
  let wasHidden=document.hidden;
  document.addEventListener('visibilitychange',async()=>{
    const returned=wasHidden&&!document.hidden;
    wasHidden=document.hidden;
    if(mounted&&!terminal())recovery.visibility();
    if(!returned||!mounted||terminal())return;
    if(snapshot.status==='idle_released'){void recovery.recover('user_activity');return;}
    try{await statusResponse(await original(activityURL,{credentials:'same-origin',cache:'no-store'}),true);}catch{}
  });
  window.addEventListener('pagehide',()=>{recovery.pause();if(pollTimer!==null)clearInterval(pollTimer);pollTimer=null;});
  window.addEventListener('pageshow',()=>{if(mounted&&!terminal()){recovery.start();if(pollTimer===null)pollTimer=setInterval(checkStatus,30000);}});
  window.addEventListener('online',()=>{if(!terminal())void recovery.recover('online');});
  document.readyState==='loading'?document.addEventListener('DOMContentLoaded',mount,{once:true}):mount();
})();

// r90: only our byte-pinned company module calls this adapter with its own ctx.
(function installNativeConnectionBinding(target) {
  'use strict';
  if (target.__MEGMEET_ATTACH_CONNECTION_R90__) return;
  const attached = new WeakSet();
  target.__MEGMEET_ATTACH_CONNECTION_R90__ = function attach(ctx) {
    const recovery = target.__MEGMEET_CLOUD_RECOVERY__;
    if (!recovery || !ctx || typeof ctx.inject !== 'function') {
      recovery?.adapterUnavailable(); return;
    }
    if (attached.has(ctx)) return;
    attached.add(ctx);
    ctx.inject(['connection'], scoped => {
      const connection = scoped.get('connection');
      if (!connection || typeof connection.state?.getSnapshot !== 'function'
          || typeof connection.generation?.getSnapshot !== 'function' || typeof connection.generation?.subscribe !== 'function'
          || typeof connection.state?.subscribe !== 'function'
          || typeof connection.reconnect !== 'function') {
        recovery.adapterUnavailable(); return;
      }
      const release = recovery.attachNative({
        getState: () => connection.state.getSnapshot(),
        getGeneration: () => connection.generation.getSnapshot()?.id,
        subscribe: listener => {
          const releaseState=connection.state.subscribe(listener),releaseGeneration=connection.generation.subscribe(listener);
          return ()=>{releaseState();releaseGeneration();};
        },
        reconnect: () => connection.reconnect(),
        // No page reload is authorized by this adapter, even for an idle page.
        canRefresh: () => false,
      });
      scoped.on('dispose', () => { release(); recovery.adapterUnavailable(); });
    });
  };
})(globalThis);
