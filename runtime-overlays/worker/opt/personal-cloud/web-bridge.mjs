// Private native web adapter -> signed-in native capability -> authenticated Gateway.
// This is deliberately not a proxy: only two reviewed connector actions exist.
import {timingSafeEqual} from 'node:crypto';
const SESSION = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const ERRORS = {
  WEB_AUTH_REQUIRED: [401, '当前联网任务的登录已失效，请重新进入云端。'],
  WEB_ARGUMENTS_INVALID: [400, '联网请求格式不正确。'],
  WEB_SEARCH_NOT_CONFIGURED: [503, '服务器尚未配置搜索服务，暂时不能按关键词搜索；可提供公开 HTTPS 网页地址进行读取。'],
  WEB_BUSY: [429, '联网请求较多，请稍后重试。'],
  WEB_CANCELLED: [409, '联网请求已停止。'],
  WEB_UNAVAILABLE: [502, '网页暂时无法读取或搜索服务不可用。仅支持无需登录的公开 HTTPS 网页。'],
};
const fail = code => { throw Object.assign(Error(code), {code}); };
const plain = value => value && typeof value === 'object' && !Array.isArray(value);
function requestBody(body) {
  if (!plain(body) || Object.keys(body).sort().join(',') !== 'action,arguments,session_id'
      || !SESSION.test(body.session_id || '') || !plain(body.arguments)) fail('WEB_ARGUMENTS_INVALID');
  const args = body.arguments;
  if (body.action === 'web_search') {
    if (Object.keys(args).sort().join(',') !== 'count,query' || typeof args.query !== 'string'
        || !args.query.trim() || args.query.length > 400 || !Number.isSafeInteger(args.count)
        || args.count < 1 || args.count > 10) fail('WEB_ARGUMENTS_INVALID');
  } else if (body.action === 'web_read') {
    if (Object.keys(args).join(',') !== 'url' || typeof args.url !== 'string'
        || args.url.length < 8 || args.url.length > 2048) fail('WEB_ARGUMENTS_INVALID');
  } else fail('WEB_ARGUMENTS_INVALID');
  return body;
}
function normalized(value, action) {
  const result = value?.result;
  if (value?.status !== 'completed' || !plain(result) || result.untrusted_external_content !== true)
    fail('WEB_UNAVAILABLE');
  if (action === 'web_search') {
    if (!Array.isArray(result.sources) || result.sources.length > 10
        || result.sources.some(x => !plain(x) || typeof x.url !== 'string' || x.url.length > 2048
          || (x.title !== undefined && typeof x.title !== 'string')
          || (x.snippet !== undefined && typeof x.snippet !== 'string'))) fail('WEB_UNAVAILABLE');
    return {sources: result.sources.map(x => ({url:x.url, ...(x.title === undefined ? {} : {title:x.title.slice(0,300)}),
      ...(x.snippet === undefined ? {} : {snippet:x.snippet.slice(0,2000)})})), truncated:false};
  }
  if (typeof result.url !== 'string' || result.url.length > 2048 || typeof result.text !== 'string'
      || result.text.length > 16000 || typeof result.truncated !== 'boolean') fail('WEB_UNAVAILABLE');
  // The Gateway reader returns only successful 200 text/HTML responses.
  return {url:result.url, statusCode:200, body:{kind:'text',content:result.text}, truncated:result.truncated};
}
export function createCloudWebBridge({token, current, validate, toolkit, consume}) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token || '')) throw Error('web bootstrap invalid');
  const active = new Map(); let tail = Promise.resolve();
  const valid = task => { if (!task || task.revoked || current(task.sessionId) !== task) fail('WEB_AUTH_REQUIRED'); };
  function stop(task) { for (const [control, owner] of active) if (!task || owner === task) control.abort(); }
  async function handle(req, res) {
    let abort, release, grant;
    try {
      if(req.method!=='POST'||req.url!=='/__megmeet/web'||req.headers.origin||req.headers['sec-fetch-site'])fail('WEB_AUTH_REQUIRED');
      grant=consume(String(req.headers.authorization||'').replace(/^Bearer /,''),'web-read');
      const task=grant.task;valid(task);
      if (active.size >= 8) fail('WEB_BUSY');
      abort = new AbortController(); active.set(abort, task);
      req.once('aborted', () => abort.abort());
      res.once('close', () => { if (!res.writableFinished) abort.abort(); });
      const signal = AbortSignal.any([abort.signal, grant.signal, AbortSignal.timeout(85000)]);
      let size=0;const chunks=[];
      for await (const chunk of req) {
        size+=chunk.length;if(size>8192)fail('WEB_ARGUMENTS_INVALID');chunks.push(chunk);
      }
      let body;try{body=requestBody(JSON.parse(Buffer.concat(chunks).toString('utf8')));}catch{fail('WEB_ARGUMENTS_INVALID');}
      if (body.session_id !== task.sessionId) fail('WEB_AUTH_REQUIRED');
      // Native multi-query search runs concurrently; the connector engine is
      // deliberately one-at-a-time. Queue at most eight, without borrowing a
      // later task's lease or extending a cancelled request's authority.
      const previous=tail;tail=new Promise(resolve=>{release=resolve;});
      await previous; signal.throwIfAborted();valid(task);
      try{await validate(task);}catch{fail('WEB_AUTH_REQUIRED');}valid(task);
      const context={clientKey:task.lease,signal};
      const value=await toolkit.nativeWeb({session_id:body.session_id,
        action:body.action,arguments:body.arguments},context);
      signal.throwIfAborted();valid(task);try{await validate(task);}catch{fail('WEB_AUTH_REQUIRED');}valid(task);
      const result=normalized(value,body.action);
      res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(result));
    } catch (error) {
      let code=Object.hasOwn(ERRORS,error?.code)?error.code:'WEB_UNAVAILABLE';
      if (error?.code==='TOOL_BUSY') code='WEB_BUSY';
      if (error?.code==='PLUGIN_SELECTION_CHANGED'||(code==='WEB_UNAVAILABLE'&&abort?.signal.aborted)) code='WEB_CANCELLED';
      const [status,message]=ERRORS[code];
      if (!res.headersSent&&!res.destroyed) {res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({code,message}));}
    } finally { if(abort)active.delete(abort);release?.();grant?.finish(); }
  }
  return {handle,stop};
}
