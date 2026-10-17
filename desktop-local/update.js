'use strict';
(function (factory) {
  const ui=factory();
  if(typeof module==='object'&&module.exports)module.exports=ui;
  else ui.mount({document,window,api:window.megmeetUpdate});
})(function () {
  const busyStates=new Set(['checking','connecting','downloading','verifying','installing','disabled']);
  const labels={idle:['自动检查更新','正在准备读取公司的软件发布信息。'],checking:['正在检查更新','正在核对公司发布的新版本。'],connecting:['正在连接下载服务','已发现新版本，正在建立下载连接。'],downloading:['正在下载更新','可以继续使用麦米；下载完成后由你确认安装。'],verifying:['正在验证更新','正在核对下载文件的完整性，验证通过后才可以安装。'],ready:['更新已准备好','已自动下载并校验。点击更新并重启即可安装；稍后再装也不影响使用。'],current:['已是最新版本','当前版本已与公司发布版本核对。'],unavailable:['本次更新未完成','可以点击重试，现有版本仍可使用。'],installing:['正在启动安装','安装窗口将显示进度，完成后自动打开新版本。请勿重复安装。'],'install-failed':['上次更新未完成','可以重试检查；现有版本不会因此被删除。'],disabled:['本地开发版','此版本不接收公司发布的自动更新。']};
  const errors={'storage-full':'磁盘可用空间不足，请释放空间后重试。','storage-permission':'无法写入本机更新目录，请检查目录权限或联系公司支持。','tls-verification':'未能验证更新服务证书，请联系公司网络支持。','signature-rejected':'更新签名校验未通过，已停止安装，请联系管理员。','clock-or-expired':'发布信息已过期或电脑时间不正确，请校对时间后重试。','rollback-rejected':'发布顺序校验未通过，已停止安装，请联系管理员。','integrity-rejected':'下载文件校验未通过，请重新检查更新。','portable-install-required':'当前为免安装版。请先安装公司发布的安装包，再使用自动更新。','network-timeout':'连接更新服务超时，请检查公司网络后重试。','check-timeout':'检查新版本超时，请确认公司网络可用后重试。','connection-timeout':'连接下载服务超时，请检查公司网络后重试。','download-stalled':'下载较长时间没有收到新数据，已停止本次下载。请检查网络后重试。','download-timeout':'下载超过本次允许的等待时间，已停止。请检查网络后重试。','verification-timeout':'更新文件核验超时，暂不能安装。请重试或联系公司支持。','check-unavailable':'暂时无法读取公司更新信息，请检查公司网络后重试。','download-unavailable':'更新下载未完成，请检查网络后重试。','installer-launch-failed':'安装器未能启动。若公司安全软件拦截，请联系公司支持开通权限后重试。'};
  const version=value=>typeof value==='string'&&/^\d{1,6}\.\d{1,6}\.\d{1,6}$/.test(value)?value:'—';
  const finite=value=>Number.isFinite(value)&&value>=0;
  const duration=seconds=>seconds>=60?Math.floor(seconds/60)+' 分 '+Math.floor(seconds%60)+' 秒':Math.floor(seconds)+' 秒';
  const mb=bytes=>(bytes/1048576).toFixed(1)+' MB';
  function view(snapshot, {now=Date.now(),action=false,unavailable=false,localError=''}={}) {
    const s=snapshot&&typeof snapshot==='object'?snapshot:{};
    const known=Object.prototype.hasOwnProperty.call(labels,s.status),status=known?s.status:'unavailable';
    const [baseTitle,baseDetail]=labels[status];
    const p=s.progress,valid=p&&Number.isSafeInteger(p.received)&&Number.isSafeInteger(p.total)&&p.total>0&&p.received>=0&&p.received<=p.total;
    const percent=valid?Math.floor(p.received*100/p.total):null;
    const stopped=['unavailable','install-failed'].includes(status),showProgress=['connecting','downloading','verifying'].includes(status)||(stopped&&valid);
    let detail=errors[s.errorCode]||baseDetail,activity='',progressText='';
    if(status==='ready'&&s.errorCode)detail='此前下载的更新已核验，可以安装；但本次检查未成功，暂未确认是否还有更新版本。';
    if(finite(s.operationStartedAt)&&s.operationStartedAt<=now&&['checking','connecting','downloading','verifying'].includes(status))activity='本次已用 '+duration((now-s.operationStartedAt)/1000);
    if(showProgress){
      if(valid){
        progressText=(stopped?'本次停在 ':'')+percent+'% · '+mb(p.received)+' / '+mb(p.total);
        if(status==='downloading'&&finite(p.bytesPerSecond)&&p.bytesPerSecond>0){
          progressText+=' · '+mb(p.bytesPerSecond)+'/秒';
          if(finite(p.remainingSeconds))progressText+=' · 预计还需 '+duration(p.remainingSeconds);
        }
      }else progressText=status==='connecting'?'连接建立后显示实际下载进度。':status==='verifying'?'正在核验文件，尚未开始安装。':'等待接收更新文件…';
      if(status==='verifying')activity='文件已下载，正在核验；下载完成不代表已安装。';
    }
    if(status==='downloading'&&finite(s.lastProgressAt)&&s.lastProgressAt<=now&&now-s.lastProgressAt>=10000){
      activity='已有 '+duration((now-s.lastProgressAt)/1000)+' 未收到新的下载数据，正在等待网络响应。';
      if(valid)progressText=percent+'% · '+mb(p.received)+' / '+mb(p.total);
    }
    if(s.cleanupPending===true){activity='正在结束上次更新连接，请稍候；完成后可以重试。';detail=detail.replace('已停止','已请求停止');}
    if(unavailable){detail='暂时无法读取本机更新状态。后台下载是否继续尚未确认，请稍后重试。';activity='';}
    return {status,title:unavailable?'更新状态暂时无法读取':baseTitle,detail,
      error:localError||(!known?'更新状态异常，请重新检查。':''),activity,showProgress:showProgress&&!unavailable,
      progress:percent,progressText,current:'当前版本 '+version(s.currentVersion),
      available:version(s.availableVersion)==='—'?'':'公司发布版本 '+version(s.availableVersion),
      checked:finite(s.checkedAt)?'上次检查：'+new Date(s.checkedAt).toLocaleString():'',
      checkDisabled:action||s.busy===true||s.cleanupPending===true||(!unavailable&&busyStates.has(status)),
      checkLabel:unavailable||localError||stopped?'重试检查':'检查更新',
      installVisible:!unavailable&&s.canInstall===true&&!s.busy&&!s.cleanupPending&&['ready','install-failed'].includes(status),
      installDisabled:action||unavailable};
  }
  function mount({document,window,api,now=Date.now,schedule=setTimeout,unschedule=clearTimeout,ipcTimeoutMs=8000,pollMs=900}) {
    const el=id=>document.getElementById(id);
    let alive=true,timer,snapshot=null,action=false,unavailable=false,localError='',epoch=0,polling=false;
    const slots=new Map(),deadlines=new Set();
    const render=()=>{
      if(!alive)return;
      const v=view(snapshot,{now:now(),action,unavailable,localError});
      for(const id of ['title','detail','activity','progress-text','current','available','checked','error'])el(id).textContent=v[id==='progress-text'?'progressText':id];
      el('available').hidden=!v.available;el('activity').hidden=!v.activity;el('error').hidden=!v.error;
      el('progress').hidden=!v.showProgress;el('progress-text').hidden=!v.showProgress;
      if(v.progress===null)el('progress').removeAttribute('value');else el('progress').value=v.progress;
      el('check').disabled=v.checkDisabled;el('check').textContent=v.checkLabel;
      el('install').hidden=!v.installVisible;el('install').disabled=v.installDisabled;
    };
    function request(kind){
      if(slots.has(kind))return slots.get(kind).visible;
      const record={epoch};slots.set(kind,record);
      const raw=Promise.resolve().then(()=>{if(!api||typeof api[kind]!=='function')throw Error('IPC_UNAVAILABLE');return api[kind]();});
      record.visible=new Promise((resolve,reject)=>{
        const deadline=schedule(()=>{deadlines.delete(deadline);reject(Error('IPC_TIMEOUT'));},ipcTimeoutMs);deadlines.add(deadline);
        raw.then(resolve,reject).finally(()=>{unschedule(deadline);deadlines.delete(deadline);if(slots.get(kind)===record)slots.delete(kind);});
      });
      // A timed-out slot is retained until the underlying IPC settles, so
      // repeated polling cannot accumulate never-returning IPC calls.
      record.visible.catch(()=>{});return record.visible;
    }
    async function refresh(){
      const generation=epoch;
      try{const promise=request('status'),issued=slots.get('status')?.epoch;const value=await promise;if(!alive||generation!==epoch||issued!==epoch)return;snapshot=value;unavailable=false;}
      catch{if(alive&&generation===epoch)unavailable=true;}
      render();
    }
    async function run(kind){
      if(action||!alive)return;
      action=true;localError='';const generation=++epoch;render();
      try{
        const value=await request(kind);if(!alive||generation!==epoch)return;
        if(kind==='check'){snapshot=value;unavailable=false;}
        else if(value?.accepted===true){snapshot={...snapshot,status:'installing',canInstall:false};}
      }catch{if(alive&&generation===epoch){localError=kind==='check'?'检查请求未完成，请重试。':'未能提交安装请求，请重试。';unavailable=true;}}
      finally{if(alive&&generation===epoch){action=false;render();void refresh();}}
    }
    async function poll(){
      if(!alive||polling)return;polling=true;
      try{await refresh();}finally{polling=false;if(alive)timer=schedule(poll,document.hidden?4000:pollMs);}
    }
    const onCheck=()=>void run('check'),onInstall=()=>void run('install');
    const onVisible=()=>{if(!alive||document.hidden)return;unschedule(timer);void poll();};
    function close(){alive=false;epoch++;unschedule(timer);for(const deadline of deadlines)unschedule(deadline);deadlines.clear();document.removeEventListener('visibilitychange',onVisible);el('check').removeEventListener('click',onCheck);el('install').removeEventListener('click',onInstall);}
    el('check').addEventListener('click',onCheck);el('install').addEventListener('click',onInstall);
    document.addEventListener('visibilitychange',onVisible);window.addEventListener('beforeunload',close,{once:true});
    render();void poll();return {refresh,close};
  }
  return {view,mount};
});
