'use strict';
// Human renderer download only. The main process supplies the current account
// credential; neither a tool result nor a URL may select an upstream/path/token.
const http = require('node:http');
const https = require('node:https');
const MAXIMUM = 32 * 1024 * 1024;
const CARD_PRINT_MAXIMUM = 128 * 1024 * 1024;
const CARD_PRINT_MEDIA = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
function artifactPath(raw) {
  if (typeof raw !== 'string' || !raw.startsWith('/__megmeet/projects/artifact?') || /[\\\r\n#]/.test(raw)) return null;
  const url = new URL(raw, 'http://127.0.0.1');
  if(url.pathname !== '/__megmeet/projects/artifact') return null;
  const names=[...url.searchParams.keys()];
  if(names.length!==3 || new Set(names).size!==3 || names.some(x=>!['session_id','attachment_id','disposition'].includes(x)))return null;
  const session=url.searchParams.get('session_id'),id=url.searchParams.get('attachment_id'),disposition=url.searchParams.get('disposition');
  if(!/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(session)||!/^pjf_[A-Za-z0-9_-]{32}$/.test(id)||!['attachment','inline'].includes(disposition))return null;
  return '/v1/personal-cloud/projects/artifact?'+new URLSearchParams({session_id:session,attachment_id:id,disposition});
}
function createProjectArtifactProxy({gateway,auth,origin,alive,browserAuthorized}) {
  const base=new URL(gateway);
  if(!['http:','https:'].includes(base.protocol)||base.username||base.password||base.search||base.hash)throw Error('INVALID_PROJECT_GATEWAY');
  const transports=new Set();let stopped=false;
  const json=(res,code)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:'project_download_unavailable'}));};
  function handle(req,res){
    if(!req.url.split('?')[0].startsWith('/__megmeet/projects/'))return false;
    const target=artifactPath(req.url);
    if(req.method!=='GET'||!target){json(res,404);return true;}
    if(!browserAuthorized(req)||req.headers.host!==new URL(origin).host||(req.headers.origin&&req.headers.origin!==origin)){json(res,403);return true;}
    if(stopped||!alive()||Date.now()>=auth.expiresAt){json(res,401);return true;}
    const outgoing=(base.protocol==='https:'?https:http).request(new URL(target,base),{method:'GET',headers:{Authorization:'Bearer '+auth.token,Origin:base.origin,Accept:'application/octet-stream'}},response=>{
      const length=response.headers['content-length'];
      // This marker is supplied only by the authenticated fixed Gateway after
      // validating the encrypted project-output registration, never by a URL
      // parameter, renderer header, filename or model-generated metadata.
      const maximum=response.statusCode===200&&response.headers['x-megmeet-artifact-kind']==='card-print-v1'&&response.headers['content-type']===CARD_PRINT_MEDIA?CARD_PRINT_MAXIMUM:MAXIMUM;
      if(response.statusCode!==200||!alive()||stopped||Date.now()>=auth.expiresAt||length&&(!/^\d+$/.test(length)||Number(length)>maximum)){response.destroy();json(res,[401,403,404,409].includes(response.statusCode)?response.statusCode:502);return;}
      const headers={'Content-Type':response.headers['content-type']||'application/octet-stream','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"sandbox; default-src 'none'"};
      if(length)headers['Content-Length']=length;
      headers['Content-Disposition']=response.headers['content-disposition']||'attachment';
      res.writeHead(200,headers);let count=0;
      response.on('data',chunk=>{count+=chunk.length;if(count>maximum||stopped||!alive()||Date.now()>=auth.expiresAt){response.destroy();res.destroy();return;}if(!res.write(chunk))response.pause();});
      res.on('drain',()=>response.resume());response.on('end',()=>res.end());response.on('error',()=>res.destroy());
    });
    transports.add(outgoing);outgoing.once('close',()=>transports.delete(outgoing));
    outgoing.setTimeout(65000,()=>outgoing.destroy());outgoing.on('error',()=>{if(!res.headersSent)json(res,502);else res.destroy();});
    req.once('aborted',()=>outgoing.destroy());res.once('close',()=>outgoing.destroy());outgoing.end();return true;
  }
  return {handle,stop(){stopped=true;for(const request of transports)request.destroy();transports.clear();}};
}
module.exports={artifactPath,createProjectArtifactProxy};
