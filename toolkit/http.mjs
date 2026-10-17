import { timingSafeEqual } from 'node:crypto';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
export function toolkitPatch(root, endpoint, token) {
  return {insert:[{id:'megmeet-selected-connectors',name:pathToFileURL(path.join(root,'adapter/index.mjs')).href,config:{endpoint,token}}]};
}
export async function readJson(req,max=32768) {
  let length=0;const chunks=[];
  for await(const chunk of req){length+=chunk.length;if(length>max)throw Object.assign(Error('REQUEST_TOO_LARGE'),{status:413});chunks.push(chunk);}
  try{return JSON.parse(Buffer.concat(chunks).toString('utf8')||'{}');}catch{throw Object.assign(Error('INVALID_JSON'),{status:400});}
}
const equal=(a,b)=>typeof a==='string'&&a.length===b.length&&timingSafeEqual(Buffer.from(a),Buffer.from(b));
// Wrapper has already validated public Host/Cookie and request origin. The
// private model path is deliberately authorized by a separate random secret.
export function createToolkitHttp(toolkit,{token,browserContext,modelContext}) {
  const actions=new Set(['catalog','state','selection','confirm','stop']);
  return async(req,res)=>{
    const url=new URL(req.url,'http://127.0.0.1');const privateCall=url.pathname==='/__megmeet/tool-execute';
    if(!privateCall&&!url.pathname.startsWith('/__megmeet/tools/'))return false;
    const reply=(status,value)=>{if(!res.destroyed){res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(JSON.stringify(value));}};
    try{
      if(privateCall){
        if(req.method!=='POST'||url.search||!equal(req.headers.authorization,'Bearer '+token))throw Object.assign(Error('TOOL_EXECUTION_DENIED'),{status:403});
        const body=await readJson(req),context=await modelContext(req,body);
        reply(200,await toolkit.execute(body,context));return true;
      }
      const action=url.pathname.slice('/__megmeet/tools/'.length);
      if(!actions.has(action))throw Object.assign(Error('NOT_FOUND'),{status:404});
      const get=action==='catalog'||action==='state';
      if(req.method!==(get?'GET':'POST'))throw Object.assign(Error('METHOD_NOT_ALLOWED'),{status:405});
      if(url.search&&!(action==='state'&&[...url.searchParams.keys()].length===1&&url.searchParams.has('session_id')))throw Object.assign(Error('QUERY_INVALID'),{status:400});
      const body=get?{session_id:url.searchParams.get('session_id')}:await readJson(req);
      const context=await browserContext(req,body);
      reply(200,await toolkit.control(action,body,context));return true;
    }catch(error){reply(error.status||400,{detail:error.code||error.message||'TOOL_UNAVAILABLE'});return true;}
  };
}
