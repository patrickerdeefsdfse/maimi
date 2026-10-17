// Private loopback office bridge: jobs stay bound to the initiating task/login.
import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { Transform, pipeline } from 'node:stream';

const PREFIX = '/__megmeet/office', ID = '[a-f0-9]{32}';
export function officeRoute(method, url) {
  if (typeof url !== 'string' || !url.startsWith(PREFIX)) return null;
  const path = url.slice(PREFIX.length);
  if (method === 'GET' && path === '/capabilities' || method === 'POST' && path === '/jobs') return path;
  if (['GET','DELETE'].includes(method) && new RegExp('^/jobs/'+ID+'$').test(path)) return path;
  if (method === 'POST' && new RegExp('^/jobs/'+ID+'/run$').test(path)) return path;
  if (method === 'PUT' && new RegExp('^/jobs/'+ID+'/inputs/(?:[0-9]|1[0-9])$').test(path)) return path;
  if (method === 'GET' && new RegExp('^/jobs/'+ID+'/outputs/(?:0|[1-9][0-9]?)$').test(path)) return path;
  return null;
}
function equal(a, b) { const aa=Buffer.from(a||''),bb=Buffer.from(b);return aa.length===bb.length&&timingSafeEqual(aa,bb); }
function limited(max, check=()=>{}) { let count=0;return new Transform({transform(chunk,_encoding,cb){
  try { check();count+=chunk.length;if(count>max)throw Error('office transfer too large');cb(null,chunk); } catch(error){cb(error);}
}}); }
function reply(res,status,detail){if(res.headersSent){res.destroy();return;}res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail}));}

export function createCloudOfficeBridge({token, base, current, validate, request=http.request}) {
  if (!/^[A-Za-z0-9_-]{43}$/.test(token) || !/^http:\/\/[a-z0-9-]+:8080\/internal\/personal-cloud\/office$/.test(base)) throw Error('office bridge configuration invalid');
  const jobs=new Map(),connections=new Map();
  const check=task=>{if(!task||task.revoked||current()!==task)throw Error('office task changed');};
  const prune=()=>{for(const [id,item] of jobs)if(Date.now()-item.at>20*60*1000)jobs.delete(id);};
  return {
    async handle(req,res){
      const route=officeRoute(req.method,req.url);
      if(!route){reply(res,404,'office_route_denied');req.resume();return;}
      if(!equal(req.headers.authorization,'Bearer '+token)){reply(res,401,'office_task_required');req.resume();return;}
      const task=current(),jobId=route.split('/')[2];
      try{
        prune();check(task);
        if(req.headers['x-megmeet-office-session']!==task.sessionId)throw Error('office session changed');
        if(jobId&&jobs.get(jobId)?.task!==task)throw Error('office job not owned');
        if(req.method==='POST'&&route==='/jobs'&&jobs.size>=64)throw Error('office job capacity');
        await validate(task);check(task);
      }catch{reply(res,401,'office_task_required');req.resume();return;}
      const upload=req.method==='PUT',download=req.method==='GET'&&route.includes('/outputs/');
      const max=upload?64*1024*1024:600*1024;
      const length=req.headers['content-length'];
      if(length!==undefined&&(!/^\d{1,12}$/.test(length)||Number(length)>max)){reply(res,413,'office_request_too_large');req.resume();return;}
      const headers={Authorization:'Bearer '+task.lease,'Content-Type':upload?'application/octet-stream':'application/json'};
      if(length!==undefined)headers['Content-Length']=length;
      const up=request(base+route,{method:req.method,headers},async out=>{
        try{
          check(task);
          if(![200,201,202].includes(out.statusCode)){
            out.resume();reply(res,[400,401,403,404,409,413,429].includes(out.statusCode)?out.statusCode:503,'office_service_unavailable');return;
          }
          if(download){
            const size=out.headers['content-length'];
            if(!/^\d{1,12}$/.test(size||'')||Number(size)>128*1024*1024)throw Error('office output invalid');
            res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Length':size,'Cache-Control':'no-store'});
            pipeline(out,limited(Number(size),()=>check(task)),res,()=>{});return;
          }
          const chunks=[];let size=0;
          for await(const chunk of out){size+=chunk.length;if(size>2*1024*1024)throw Error('office response too large');chunks.push(chunk);}
          check(task);
          const data=Buffer.concat(chunks),value=JSON.parse(data.toString('utf8'));
          if(route==='/jobs'){
            if(value.protocol!=='megmeet-office-remote-v1'||!new RegExp('^'+ID+'$').test(value.job_id))throw Error('office job invalid');
            jobs.set(value.job_id,{task,at:Date.now()});
          }
          if(req.method==='DELETE')jobs.delete(jobId);
          res.writeHead(out.statusCode,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(data);
        }catch{out.destroy();reply(res,502,'office_response_invalid');}
      });
      connections.set(up,task);
      up.setTimeout(65000,()=>up.destroy());
      up.on('error',()=>reply(res,503,'office_service_unavailable'));
      up.once('close',()=>connections.delete(up));
      req.once('aborted',()=>up.destroy());res.once('close',()=>up.destroy());
      pipeline(req,limited(max,()=>check(task)),up,error=>{if(error)reply(res,413,'office_transfer_failed');});
    },
    stop(task=null){for(const [req,owner]of connections)if(!task||owner===task)req.destroy();},
  };
}
