/* Hand-authored standard DSH client bundle; no replacement of native modules. */
window.__ModuleLoader__.load({
 id:'maimi-managed-plugins',
 factory:require=>{
  const {createElement,useState,useEffect}=require('react');
  const outputWarning='部分文件仍在临时工作区，尚未保存到云端，请稍后重试或手动下载。';
  /** Session-scoped, same-origin, bounded status read. Desktop hosts do not
   * register the route; a 404/405 stops this feature for the page lifetime. */
  function createOutputStatusMonitor({fetcher=(...args)=>fetch(...args),doc=document,now=()=>Date.now(),schedule=setTimeout,cancel=clearTimeout}={}){
   const sessions=new Map();let disabled=false,watchers=0;
   const stop=row=>{if(row.timer!==null)cancel(row.timer);row.timer=null;};
   const emit=row=>{for(const fn of row.listeners)fn(row.value);};
   const later=row=>{stop(row);if(!disabled&&!doc.hidden&&row.listeners.size)row.timer=schedule(()=>{row.timer=null;void poll(row);},Math.max(1,10000-(now()-row.last)));};
   async function poll(row){
    if(disabled||doc.hidden||!row.listeners.size||row.busy)return;
    if(now()-row.last<10000){later(row);return;}
    row.last=now();row.busy=true;row.controller=new AbortController();
    try{
     const target=new URL('api/maimi/outputs/status',doc.baseURI);
     if(target.origin!==location.origin)throw Error('invalid status origin');
     const response=await fetcher(target,{method:'POST',credentials:'same-origin',redirect:'error',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({sessionId:row.id}),signal:AbortSignal.any([row.controller.signal,AbortSignal.timeout(8000)])});
     if(response.status===404||response.status===405){disabled=true;for(const item of sessions.values()){stop(item);item.controller?.abort();item.value=null;emit(item);}return;}
     if(!response.ok)throw Error('output status unavailable');
     const value=await response.json();if(!value||!['idle','saving','saved','attention'].includes(value.status))throw Error('output status invalid');
     row.value=value.status==='attention'?{message:outputWarning}:null;emit(row);
    }catch(error){if(!row.controller.signal.aborted){row.value={message:'暂时无法确认文件是否已保存到云端，请稍后检查或下载。'};emit(row);}}
    finally{row.busy=false;row.controller=null;later(row);}
   }
   function visible(){for(const row of sessions.values()){stop(row);if(doc.hidden)row.controller?.abort();else void poll(row);}}
   return {watch(id,listener){
    if(disabled||typeof id!=='string'||!/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(id))return()=>{};
    let row=sessions.get(id);if(!row){if(sessions.size>=256){const stale=[...sessions.values()].find(x=>!x.listeners.size&&!x.busy);if(!stale)return()=>{};sessions.delete(stale.id);}row={id,listeners:new Set(),value:null,last:-Infinity,timer:null,busy:false,controller:null};sessions.set(id,row);}
    row.listeners.add(listener);listener(row.value);if(watchers++===0)doc.addEventListener('visibilitychange',visible);void poll(row);
    return()=>{if(!row.listeners.delete(listener))return;watchers--;if(!row.listeners.size){stop(row);row.controller?.abort();}if(!watchers)doc.removeEventListener('visibilitychange',visible);};
   }};
  }
  let outputMonitor;
  function OutputStatus({sessionId}){
   const [state,setState]=useState(null);
   useEffect(()=>{outputMonitor??=createOutputStatusMonitor();return outputMonitor.watch(sessionId,setState);},[sessionId]);
   return state?createElement('span',{role:'alert',style:{color:'#a65100',fontSize:'12px',whiteSpace:'normal',maxWidth:'30rem'},'data-maimi-output-status':'attention'},state.message):null;
  }
  /** Input is supplied by the native session-scoped file-action slot. Never
   * infer the owner from the active tab, DOM text, or a session-list response. */
  function NativeFileScope({sessionId,actionUrl}) {
   if(typeof sessionId!=='string'||!/^[A-Za-z0-9._:-]{1,200}$/.test(sessionId)||typeof actionUrl!=='string')return null;
   let url;try{url=new URL(actionUrl,document.baseURI);}catch{return null;}
   if(url.origin!==location.origin||!/(?:\/api\/present\.open|\/api\/changes\.open)$/.test(url.pathname))return null;
   if(url.searchParams.getAll('sessionId').length!==1||url.searchParams.get('sessionId')!==sessionId)return null;
   for(const key of ['seq','index'])if(url.searchParams.getAll(key).length!==1||!/^\d+$/.test(url.searchParams.get(key)??''))return null;
   return createElement('span',{hidden:true,'data-maimi-file-scope':'native-session-slot','data-maimi-session-id':sessionId,'data-maimi-file-seq':url.searchParams.get('seq'),'data-maimi-file-index':url.searchParams.get('index')});
  }
  return {inject:['slots'],apply(ctx){
   for(const name of ['deliverables.file.actions','deliverables.review.file.actions'])ctx.slots.inject(name,()=>ctx.slots.register({name,id:'maimi-download-scope',order:-100},NativeFileScope));
   for(const name of ['deliverables.file.actions','deliverables.review.file.actions','conversation.session.header.actions'])ctx.slots.inject(name,()=>ctx.slots.register({name,id:'maimi-output-status',order:100},OutputStatus));
  },NativeFileScope,OutputStatus,createOutputStatusMonitor};
 }
});
