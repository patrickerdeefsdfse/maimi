// Managed native web capability. Employee IAM is owned by Electron; this module
// holds only its private, per-account loopback token, never a TokenKey key.
export const name = 'megmeet-desktop-search';
export const inject = ['agents', 'web'];
export const PROVIDER = 'megmeet-managed-search';
const SESSION = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const fail = (code, message=code) => { throw Object.assign(Error(message),{code}); };
export function initiatingRoot(agents) {
  let agent=agents.currentInitiator();
  const live=agents.list(),roots=new Set(agents.roots()),seen=new Set();
  if(live.length>512)fail('WEB_SESSION_REQUIRED');
  while(agent&&live.includes(agent)&&!seen.has(agent)) {
    seen.add(agent);
    if(roots.has(agent)) {
      if(SESSION.test(agent.session?.id||'')&&agent.id===agent.session.id)return agent.session.id;
      break;
    }
    const owners=live.filter(owner=>agents.isOwnedBy(agent.id,owner));
    if(owners.length!==1)break;
    agent=owners[0];
  }
  fail('WEB_SESSION_REQUIRED');
}
async function boundedJson(response) {
  let size=0;const chunks=[];
  for await(const chunk of response.body||[]) {
    size+=chunk.byteLength;if(size>128*1024)fail('WEB_RESPONSE_TOO_LARGE');chunks.push(Buffer.from(chunk));
  }
  try{return JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{fail('WEB_RESPONSE_INVALID');}
}
function publicHttps(value) {
  if(typeof value!=='string'||value.length>2048)return false;
  try{const u=new URL(value);return u.protocol==='https:'&&!u.username&&!u.password;}catch{return false;}
}
export function apply(ctx,config) {
  const endpoint=new URL(config.endpoint);
  if(endpoint.protocol!=='http:'||endpoint.hostname!=='127.0.0.1'||!endpoint.port
    ||endpoint.pathname!=='/__megmeet/native-web'||endpoint.search||endpoint.hash
    ||endpoint.username||endpoint.password||!/^[A-Za-z0-9_-]{43}$/.test(config.token||''))fail('SEARCH_PRIVATE_ENDPOINT_REQUIRED');
  const queues=new Map(),stop=new AbortController();
  const call=async(action,args,callerSignal)=>{
    const session=initiatingRoot(ctx.agents),signal=AbortSignal.any([callerSignal,stop.signal,AbortSignal.timeout(90000)].filter(Boolean));
    signal.throwIfAborted();
    let queue=queues.get(session);
    if(!queue){queue={tail:Promise.resolve(),count:0};queues.set(session,queue);}
    if(queue.count>=8)fail('WEB_BUSY','本条对话的联网请求较多，请稍后重试。');
    const previous=queue.tail;let release;
    queue.tail=new Promise(resolve=>{release=resolve;});queue.count++;
    try {
      await previous;signal.throwIfAborted();
      if(initiatingRoot(ctx.agents)!==session)fail('WEB_SESSION_REQUIRED');
      const response=await fetch(endpoint,{method:'POST',redirect:'error',
        headers:{'Content-Type':'application/json',Authorization:'Bearer '+config.token},
        body:JSON.stringify({session_id:session,action,arguments:args}),signal});
      const value=await boundedJson(response);signal.throwIfAborted();
      if(initiatingRoot(ctx.agents)!==session)fail('WEB_SESSION_REQUIRED');
      if(!response.ok)fail(/^[A-Z_]{1,100}$/.test(value.detail||'')?value.detail:'WEB_UNAVAILABLE','联网暂未完成，请稍后重试；无需填写 API 密钥。');
      if(value.status!=='completed'||value.result?.untrusted_external_content!==true)fail('WEB_RESPONSE_INVALID');
      return value.result;
    }finally{release();if(--queue.count===0&&queues.get(session)===queue)queues.delete(session);}
  };
  const releaseSearch=ctx.web.registerSearchProvider({id:PROVIDER,available:()=>!stop.signal.aborted,
    async search(request,signal) {
      if(typeof request?.query!=='string'||!request.query.trim()||request.query.length>400
        ||request.maxResults!==undefined&&(!Number.isSafeInteger(request.maxResults)||request.maxResults<1||request.maxResults>10))fail('WEB_ARGUMENTS_INVALID');
      const result=await call('web_search',{query:request.query,count:request.maxResults??5},signal);
      if(!Array.isArray(result.sources)||!result.sources.length||result.sources.length>10||result.sources.some(x=>!x||!publicHttps(x.url)))fail('WEB_RESPONSE_INVALID');
      const provider=result.search_provider;
      const notice=provider==='deepseek-official'?'搜索服务：DeepSeek 官方搜索。'
        :['bing','bing-public'].includes(provider)?'搜索服务：Bing（备用检索；官方搜索当前不可用）。'
        :'搜索服务：企业搜索（服务端未报告具体来源，不能认定为 DeepSeek 官方搜索）。';
      return {sources:result.sources.slice(0,request.maxResults??5).map(x=>({url:x.url,
        ...(typeof x.title==='string'?{title:x.title.slice(0,300)}:{}),...(typeof x.snippet==='string'?{snippet:x.snippet.slice(0,2000)}:{})})),
        truncated:result.truncated===true,content:notice,...(typeof provider==='string'?{search_provider:provider}:{}),
        ...(/^[A-Z_]{1,100}$/.test(result.fallback_reason||'')?{fallback_reason:result.fallback_reason}:{})};
    }});
  const releaseFetch=ctx.web.registerFetchProvider({id:PROVIDER,available:()=>!stop.signal.aborted,
    async fetch(request,signal) {
      if(!publicHttps(request?.url))fail('WEB_ARGUMENTS_INVALID');
      const result=await call('web_read',{url:request.url},signal);
      if(!publicHttps(result.url)||typeof result.text!=='string'||result.text.length>16000||typeof result.truncated!=='boolean')fail('WEB_RESPONSE_INVALID');
      return {url:result.url,statusCode:200,body:{kind:'text',content:result.text},truncated:result.truncated};
    }});
  return ()=>{stop.abort();releaseSearch?.();releaseFetch?.();};
}
