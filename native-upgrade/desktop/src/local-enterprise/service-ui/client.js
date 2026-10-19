__ModuleLoader__.load({id:'maimi-local-service-ui',factory(require){
 const React=require('react'),{createElement:h,useEffect,useRef,useState}=React;
 return {inject:['slots','connection'],apply(ctx){
  function ServiceConnection({sessionId,useSession}){
   const session=useSession?.(v=>v),id=sessionId||session?.id||session?.sessionId;
   const [state,setState]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
   const form=useRef(null),active=useRef(null),alive=useRef(true);
   async function perform(method,fields={}){
    if(!id||active.current)return;
    const controller=new AbortController();active.current=controller;if(method!=='state'){setBusy(true);setError('');}
    try{
     const result=await ctx.connection.rpc.call('/maimi-service',method,{session_id:id,...fields},AbortSignal.any([controller.signal,AbortSignal.timeout(35000)]));
     if(!result.ok)throw Error(result.error?.message||'SERVICE_UNAVAILABLE');
     if(alive.current)setState(result.value);
    }catch(e){if(alive.current&&!controller.signal.aborted&&method!=='state')setError('连接未完成，请检查地址和密码后重试。');}
    finally{fields.password='';if(method==='connect'&&form.current?.elements.namedItem('password'))form.current.elements.namedItem('password').value='';if(active.current===controller)active.current=null;if(alive.current&&method!=='state')setBusy(false);}
   }
   useEffect(()=>{alive.current=true;setState(null);void perform('state');const timer=setInterval(()=>{if(document.visibilityState!=='hidden')void perform('state');},5000);return()=>{alive.current=false;clearInterval(timer);active.current?.abort();active.current=null;if(form.current?.elements.namedItem('password'))form.current.elements.namedItem('password').value='';};},[id]);
   if(!id)return null;
   if(!state?.enabled)return h('div',{'data-maimi-local-service':''},h('button',{type:'button',disabled:busy,onClick:()=>perform('enable')},'service · 连接服务器'),error&&h('span',{role:'alert'},error));
   const style={padding:'9px 11px',border:'1px solid #b9cac3',borderRadius:8,font:'inherit',minWidth:0};
   return h('section',{'aria-label':'连接服务器','data-maimi-local-service':'',style:{padding:14,marginBottom:10,border:'1px solid #b9cac3',borderRadius:12}},
    h('strong',null,state.connected?'服务器已连接':'连接服务器'),
    state.connected?h('div',null,h('p',{role:'status'},state.server?.label||'已连接'),h('button',{type:'button',disabled:busy,onClick:()=>perform('disable')},'断开连接')):
     h('form',{ref:form,autoComplete:'off',onSubmit:event=>{event.preventDefault();if(busy)return;const target=event.currentTarget,ip=target.elements.namedItem('ip').value.trim(),password=target.elements.namedItem('password').value;target.elements.namedItem('password').value='';void perform('connect',{ip,password});},style:{display:'flex',gap:8,flexWrap:'wrap',paddingTop:10}},
      h('label',{style:{display:'grid',gap:4}},'IP 地址',h('input',{name:'ip','aria-label':'IP 地址',type:'text',required:true,maxLength:15,inputMode:'decimal',autoComplete:'off',spellCheck:false,disabled:busy,style})),
      h('label',{style:{display:'grid',gap:4}},'密码',h('input',{name:'password','aria-label':'密码',type:'password',required:true,maxLength:4096,autoComplete:'off',disabled:busy,style})),
      h('button',{type:'submit',disabled:busy,style:{...style,alignSelf:'end'}},busy?'连接中…':'连接')),
    error&&h('p',{role:'alert'},error));
  }
  ctx.slots.inject('conversation.input.dock',()=>ctx.slots.register({name:'conversation.input.dock',id:'maimi-local-service',order:10},ServiceConnection));
 }};
}});
