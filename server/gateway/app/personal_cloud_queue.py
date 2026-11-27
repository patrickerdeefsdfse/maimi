"""Browser admission UI. No user task is replayed by connection recovery."""

ENTRY_SCRIPT = r"""
const status = document.getElementById('state');
const login = document.getElementById('login');
const retry = document.getElementById('retry');
const cancelQueue = document.createElement('button');
cancelQueue.type='button';cancelQueue.className='download';cancelQueue.hidden=true;
cancelQueue.textContent='取消排队';retry.after(cancelQueue);
const loginURL='/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F';
const attemptKey='megmeet.web.iam-attempt.v1';
let active=false, epoch=0, queueRequest=null, deadline=0;
const reentry = new URLSearchParams(location.search).get('recover');
let handoff = reentry === '1';
const delayFor = ms => new Promise(resolve => setTimeout(resolve,ms));
function forgetAttempt(){try{sessionStorage.removeItem(attemptKey);}catch{}}
function requireLogin(){
  login.hidden=false;status.textContent='请通过公司 IAM 登录，已登录的账号将尝试自动进入。';
  if(document.body.dataset.autoLogin!=='true')return;
  try{const raw=sessionStorage.getItem(attemptKey),last=raw===null?NaN:Number(raw),now=Date.now();
    if(Number.isFinite(last)&&(last>now||now-last<120000))return;
    sessionStorage.setItem(attemptKey,String(now));if(sessionStorage.getItem(attemptKey)!==String(now))return;
  }catch{return;}
  location.replace(loginURL);
}
login.onclick=forgetAttempt;
async function cancelWaiting(){
  if(!queueRequest)return;
  const request_id=queueRequest;epoch++;active=false;cancelQueue.hidden=true;retry.hidden=true;
  try{
    const response=await fetch('/v1/personal-cloud/queue/cancel',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id}),
      credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000)});
    if(!response.ok)throw Error();
    const value=await response.json();
    status.textContent=value.status==='cancelled'?'已取消排队。聊天和文件仍然保留。':'这次排队已经结束，可能已开始准备工作区。可点击重新连接。';
  }catch{status.textContent='取消结果暂未确认，已停止本页轮询。等待请求会到期，可稍后重新连接。';}
  retry.hidden=false;
}
cancelQueue.onclick=()=>void cancelWaiting();
async function enter() {
  if(active)return;
  active=true;const current=++epoch;queueRequest=null;deadline=Date.now()+300000;
  retry.hidden=true;login.hidden=true;cancelQueue.hidden=true;
  let interruptions=0;
  try{
    while(current===epoch&&Date.now()<deadline){
      status.textContent=queueRequest?'正在确认排队进度…':'正在连接个人云端工作区…';
      let response;
      try {
        globalThis.__MEGMEET_ENTRY_TIMING__?.mark('workspace_request');
        response = await fetch('/v1/personal-cloud/open', {
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(queueRequest?{queue_request_id:queueRequest}:{}),
        credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(15000),
        });
      } catch (err) {
        if (!(err instanceof TypeError || ['TimeoutError','AbortError','NetworkError'].includes(err?.name))) throw err;
      }
      if(current!==epoch)return;
      if(!response || [502,503,504].includes(response.status)){
        if(response?.status===503){
          const problem=await response.json().catch(()=>null);
          if(current!==epoch)return;
          if(problem?.detail==='personal_cloud_worker_start_exited'){
            status.textContent='工作区启动进程已退出。请点击重试，重新打开原工作区。';retry.hidden=false;return;
          }
        }
        if(++interruptions>4){
          status.textContent='工作区暂未就绪。请稍后点击重试，继续打开本次工作区。';retry.hidden=false;return;
        }
        status.textContent='工作区仍在启动，正在重新检查…';
        await delayFor(Math.min(1000*2**interruptions,10000,Math.max(0,deadline-Date.now())));
        continue;
      }
      interruptions=0;
      if(response.status===401){requireLogin();return;}
      if(response.status===403){status.textContent='当前账号暂时无法使用麦米，请联系管理员确认授权。';return;}
      if(response.status===409||response.status===429){
        const value=await response.json();
        if(current!==epoch)return;
        status.textContent=response.status===429?'当前等候人数较多，请稍后重新连接。':
          value.detail==='personal_cloud_queue_cancelled'?'这次排队已取消，可点击重新连接。':'这次等待已结束，请点击重新连接。';
        retry.hidden=false;return;
      }
      if(response.status===202){
        const value=await response.json();
        if(current!==epoch)return;
        if(value.status==='queued'){
          if(!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.request_id)||
              !Number.isInteger(value.position)||value.position<1||!Number.isFinite(value.expires_at))throw Error();
          if(queueRequest&&queueRequest!==value.request_id)throw Error();
          queueRequest=value.request_id;deadline=Math.min(deadline,value.expires_at*1000);
          cancelQueue.hidden=false;
          status.textContent='云端工作区正在排队（第 '+value.position+' 位），空闲后会进入。最多等待 5 分钟。';
        }else if(value.status==='starting'&&value.phase==='preparing'){
          cancelQueue.hidden=true;status.textContent='正在准备云端工作区，请稍候…';
        }else throw Error();
        const delay=value.retry_after===2||value.status==='queued'?2000:1000;
        await new Promise(resolve=>setTimeout(resolve,delay));continue;
      }
      if(!response.ok)throw Error();
      const opened=await response.json();
      if(current!==epoch)return;
      if(!opened || typeof opened.workspace_id!=='string' || !opened.workspace_id ||
         typeof opened.native_workspace_id!=='string' || !opened.native_workspace_id) throw Error();
      forgetAttempt();
      globalThis.__MEGMEET_ENTRY_TIMING__?.mark('workspace_ready');
      location.replace(handoff?'/maimi/web/chat?recover=1':'/maimi/web/chat');
      return;
    }
    if(current===epoch){status.textContent='本次等待已结束。登录、聊天和文件仍保留，请点击重新连接。';retry.hidden=false;}
  }catch{if(current===epoch){status.textContent='暂时无法连接云端，请稍后重新连接。';retry.hidden=false;}}
  finally{if(current===epoch){active=false;cancelQueue.hidden=true;}}
}
retry.onclick=()=>{handoff=false;void enter();};
if(new URLSearchParams(location.search).get('idle')==='1'){
  status.textContent='云端工作区已释放。点击继续，重新进入你的聊天和文件。';
  retry.textContent='继续使用';retry.hidden=false;
}else if(reentry==='2'){
  status.textContent='工作区已响应，但聊天页面暂未就绪。请点击重试。';retry.hidden=false;
}else{void enter();}
"""


def with_queue_recovery(source):
    """Narrowly extend the existing, tested transport recovery state machine."""
    changes = [
        ("let presencePending = false, generation = 0, busyCheckedAt = 0;",
         "let presencePending = false, generation = 0, busyCheckedAt = 0;\n    let queueRequest=null, queueDeadline=0, queueStopped=false;"),
        ("if(response.status===409)void recover('host_missing');",
         "if(response.status===409&&!queueStopped){queueStopped=true;publish({status:'idle_released'});}"),
        ("if(!active()||!hooks)return;", "if(!active()||!hooks||queueStopped)return;"),
        ("if(!active()||options.online?.()===false)return false;\n      if(pending)return pending;",
         "if(!active()||options.online?.()===false)return false;\n      if(pending)return pending;\n      if(reason==='manual'){queueStopped=false;queueRequest=null;queueDeadline=0;budget={started:now(),attempts:0,refreshes:0};}\n      else if(queueStopped)return false;"),
        ("const until=now()+120000;", "const until=queueDeadline||now()+300000;"),
        ("          while(active()&&generation===current&&now()<until) {",
         "          if(reason!=='manual'&&!queueRequest){\n            const probe=await request(options.presenceURL,{client_id:options.clientId,present:true});\n            if(!active()||generation!==current)return false;\n            if(auth(probe.status))return false;\n            if(probe.status===409){queueStopped=true;publish({status:'idle_released'});return false;}\n            if(!probe.ok)throw Error('HOST_STATE_UNKNOWN');\n          }\n          while(active()&&generation===current&&now()<until) {"),
        ("const response=await request(options.openURL,{});",
         "if(queueDeadline&&now()>=queueDeadline){queueStopped=true;publish({status:'queue_expired'});return false;}\n            const response=await request(options.openURL,queueRequest?{queue_request_id:queueRequest}:{});"),
        ("if(response.status===202) {\n              const value=response.value;",
         "if(response.status===409||response.status===429){queueStopped=true;publish({status:'queue_expired'});return false;}\n            if(response.status===202) {\n              const value=response.value;\n              if(value?.status==='queued'){\n                if(!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.request_id)||!Number.isFinite(value.expires_at)||!Number.isInteger(value.position)||value.position<1||(queueRequest&&queueRequest!==value.request_id))throw Error('INVALID_QUEUE');\n                queueRequest=value.request_id;queueDeadline=Math.min(until,value.expires_at*1000);\n                publish({status:'queued',queuePosition:value.position});\n              }"),
        ("hooks?.reconnect();\n            publish", "queueRequest=null;queueDeadline=0;\n            hooks?.reconnect();\n            publish"),
        ("throw Error('OPEN_TIMEOUT');", "queueStopped=true;publish({status:'queue_expired'});return false;"),
        ("if(active()&&generation===current) {\n            publish",
         "if(active()&&generation===current&&!queueStopped) {\n            if(queueRequest){queueStopped=true;publish({status:'queue_expired'});return false;}\n            publish"),
        ("recover,attachNative,auth,start,pause,", "recover,attachNative,auth,start,pause,\n      cancelQueue:async()=>{\n        if(!queueRequest)return;const request_id=queueRequest;queueStopped=true;generation++;clearTimers();publish({status:'queue_cancelled'});\n        try{const response=await request('/v1/personal-cloud/queue/cancel',{request_id});if(!response.ok)publish({status:'queue_cancel_unconfirmed'});}catch{publish({status:'queue_cancel_unconfirmed'});}\n      },"),
        ("recovering:'正在恢复连接，当前输入会保留…',", "recovering:'正在恢复连接，当前输入会保留…',\n      queued:'正在等待空闲工作区（第 '+(snapshot.queuePosition||1)+' 位）。当前输入会保留，最多等待 5 分钟。',\n      queue_expired:'这次等待已结束，当前输入会保留。请点击重新连接。',\n      queue_cancelled:'已停止排队，当前输入会保留。',\n      queue_cancel_unconfirmed:'取消结果尚未确认，已停止轮询。等待请求会到期。',"),
        ("}else if(['recovery_failed','refresh_waiting'].includes(snapshot.status)){",
         "}else if(snapshot.status==='queued'){\n      const button=document.createElement('button');button.textContent='取消排队';button.onclick=()=>void recovery.cancelQueue();panel.append(button);\n    }else if(['recovery_failed','refresh_waiting','queue_expired','queue_cancelled','queue_cancel_unconfirmed'].includes(snapshot.status)){"),
    ]
    for before, after in changes:
        if source.count(before) != 1:
            raise RuntimeError('CLOUD_RECOVERY_SOURCE_CHANGED')
        source = source.replace(before, after, 1)
    source = source.replace("      queued:'正在等待空闲工作区", "      idle_released:'工作区已因空闲释放，聊天和文件仍保留。点击重新连接继续使用。',\n      queued:'正在等待空闲工作区", 1)
    source = source.replace("['recovery_failed','refresh_waiting','queue_expired'", "['recovery_failed','refresh_waiting','idle_released','queue_expired'", 1)
    return source
