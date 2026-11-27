// The child gets no browser/worker lease. Resolve identity in the trusted parent.
import {randomUUID} from 'node:crypto';
const SESSION=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
export class AttachmentScopes {
 constructor({current,validate}){this.entries=new Map();this.current=current;this.validate=validate;}
 async register(session,lease){
  if(!SESSION.test(session||''))throw Error('attachment session invalid');
  await this.validate({lease});const key=randomUUID();this.entries.set(key,{session,lease});
  return ()=>this.entries.delete(key);
 }
 async resolve(session){
  if(session&&!SESSION.test(session))throw Error('attachment session invalid');
  const matches=[...this.entries.values()].filter(v=>session&&v.session===session);
  if(matches.length){
   if(new Set(matches.map(v=>v.lease)).size!==1)throw Error('attachment scope ambiguous');
   await this.validate({lease:matches[0].lease});return matches[0];
  }
  const active=this.current(session||null);
  if(active&&(!session||active.sessionId===session)&&!active.revoked){await this.validate(active);return {session:active.sessionId,lease:active.lease};}
  throw Error('attachment scope not active');
 }
}

export async function relayAttachment(req,res,{scopes,token,modelBase,readJson}){
 if(req.method!=='POST'||req.headers.origin||req.headers['sec-fetch-site']||req.headers.authorization!=='Bearer '+token)throw Error('attachment bridge denied');
 const operation=req.url.slice('/__megmeet/attachments/'.length);
 if(!['put','resolve','read'].includes(operation))throw Error('attachment bridge denied');
 const scope=await scopes.resolve(req.headers['x-maimi-session-id']);
 const headers={Authorization:'Bearer '+scope.lease,'X-Maimi-Session-ID':scope.session};
 let path='/internal/storage/native/',body,method='POST';
 if(operation==='put'){
  const raw=req.headers['content-length'];if(!/^[0-9]{1,8}$/.test(raw||'')||Number(raw)>32*1024*1024)throw Error('attachment length invalid');
  const parts=[];let size=0;for await(const part of req){size+=part.length;if(size>Number(raw))throw Error('attachment length invalid');parts.push(part);}
  if(size!==Number(raw))throw Error('attachment length invalid');body=Buffer.concat(parts);path+='files';
  for(const name of ['content-length','content-type','x-maimi-request-id','x-maimi-filename','x-maimi-sha256'])if(req.headers[name])headers[name]=req.headers[name];
 }else{
  const value=await readJson(req,4096);
  if(operation==='read'){
   if(Object.keys(value).length!==1||!/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.file_id||''))throw Error('attachment read invalid');
   path+='files/'+value.file_id;method='GET';
  }else{path+='resolve';headers['Content-Type']='application/json';body=JSON.stringify(value);}
 }
 // Recheck after body transfer, before quota admission/publication.
 await scopes.resolve(scope.session);
 const response=await fetch(new URL(path,modelBase),{method,redirect:'error',headers,body,signal:AbortSignal.timeout(30000)});
 const bytes=Buffer.from(await response.arrayBuffer());if(bytes.length>32*1024*1024)throw Error('attachment response too large');
 res.writeHead(response.status,{'Content-Type':response.headers.get('content-type')||'application/json','Content-Length':String(bytes.length),'Cache-Control':'no-store'});res.end(bytes);
}
