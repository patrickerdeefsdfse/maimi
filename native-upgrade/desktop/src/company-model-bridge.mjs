import http from 'node:http';
import crypto from 'node:crypto';

const MAX_BODY = 16 * 1024 * 1024;
const deny = (res, status, code) => { res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' }); res.end(JSON.stringify({error:{code}})); };
function active(auth) {
  return auth && typeof auth.token === 'string' && auth.token.length > 0 && typeof auth.owner === 'string' && auth.owner.length > 0 && Number.isSafeInteger(auth.generation) && auth.expiresAt > Date.now() && Array.isArray(auth.models) && auth.models.every(x => typeof x.id === 'string');
}

/** Parent-process credential boundary: the native Host sees only an ephemeral
 * loopback capability. Company access tokens stay in getAuthorization(). */
export async function createCompanyModelBridge({ getAuthorization, gatewayOrigin, testOnly = false, fetchImpl = fetch }) {
  const gateway = new URL(gatewayOrigin);
  const loopback = gateway.protocol === 'http:' && gateway.hostname === '127.0.0.1' && !!gateway.port;
  if (gateway.origin !== gatewayOrigin || gateway.username || gateway.password || !(gatewayOrigin === 'https://platform-ai.megmeet.com' || testOnly && loopback)) throw Error('GATEWAY_ORIGIN_NOT_ALLOWED');
  if (typeof getAuthorization !== 'function') throw Error('AUTHORIZATION_CALLBACK_REQUIRED');
  const token = crypto.randomBytes(32).toString('base64url');
  const requests = new Set();
  let closed = false;
  let closing;
  const server = http.createServer(async (req, res) => {
    if (closed || req.headers.authorization !== `Bearer ${token}` || req.headers.origin) return deny(res, 401, 'BRIDGE_AUTH_REQUIRED');
    if (req.url !== '/v1/models' && req.url !== '/v1/chat/completions') return deny(res, 404, 'NOT_FOUND');
    let owner;
    try { owner = await getAuthorization(); }
    catch { return deny(res, 401, 'COMPANY_SESSION_UNAVAILABLE'); }
    if (!active(owner)) return deny(res, 401, 'COMPANY_SESSION_EXPIRED');
    // Freeze this request's identity even when the parent renews a mutable object.
    owner={...owner,models:owner.models.map(model=>({...model}))};
    if (req.url === '/v1/models') {
      if (req.method !== 'GET') return deny(res, 405, 'METHOD_NOT_ALLOWED');
      res.writeHead(200, {'content-type':'application/json','cache-control':'no-store'});
      return res.end(JSON.stringify({data:owner.models.map(({id,contextWindow,maxTokens})=>({id,object:'model',contextWindow,maxTokens}))}));
    }
    if (req.method !== 'POST') return deny(res, 405, 'METHOD_NOT_ALLOWED');
    const controller = new AbortController(); requests.add(controller);
    const timeout = setTimeout(()=>controller.abort(), 120000);timeout.unref();
    req.on('aborted', ()=>controller.abort());res.on('close',()=>controller.abort());
    try {
      const chunks=[];let size=0;
      for await (const part of req) {size+=part.length;if(size>MAX_BODY){deny(res,413,'REQUEST_TOO_LARGE');return;}chunks.push(part);}
      const bytes=Buffer.concat(chunks);let body;
      try {body=JSON.parse(bytes);} catch {deny(res,400,'INVALID_JSON');return;}
      if (!body || !owner.models.some(x=>x.id===body.model)) {deny(res,403,'MODEL_NOT_AUTHORIZED');return;}
      const current=await getAuthorization();
      if (!active(current) || current.owner!==owner.owner || current.generation!==owner.generation || current.token!==owner.token) {deny(res,401,'COMPANY_SESSION_CHANGED');return;}
      const response=await fetchImpl(`${gatewayOrigin}/v1/personal-model/chat/completions`,{method:'POST',headers:{authorization:`Bearer ${owner.token}`,'content-type':'application/json'},body:bytes,signal:controller.signal,redirect:'error'});
      const after=await getAuthorization();
      if (!active(after)||after.owner!==owner.owner||after.generation!==owner.generation||after.token!==owner.token) {controller.abort();deny(res,401,'COMPANY_SESSION_CHANGED');return;}
      if (!response.ok) {await response.body?.cancel();deny(res,response.status>=400&&response.status<=599?response.status:502,'COMPANY_MODEL_REQUEST_FAILED');return;}
      res.writeHead(response.status,{'content-type':body.stream?'text/event-stream':'application/json','cache-control':'no-store'});
      if(response.body)for await(const part of response.body){
        const state=await getAuthorization();
        if(!active(state)||state.owner!==owner.owner||state.generation!==owner.generation||state.token!==owner.token){controller.abort();res.destroy();return;}
        if (!res.write(part)) await new Promise((resolve,reject)=>{
          const drained=()=>{res.off('close',closed);resolve();};
          const closed=()=>{res.off('drain',drained);reject(Error('CLOSED'));};
          res.once('drain',drained);res.once('close',closed);
        });
      }
      res.end();
    } catch {if(!res.headersSent)deny(res,502,'COMPANY_MODEL_UNAVAILABLE');else res.destroy();}
    finally {clearTimeout(timeout);requests.delete(controller);}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  const address=server.address();
  return { origin:`http://127.0.0.1:${address.port}`, capability:token, close:()=>closing??=(async()=>{closed=true;for(const request of requests)request.abort();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));})() };
}
