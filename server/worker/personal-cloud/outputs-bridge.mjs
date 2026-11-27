// Allocated output directories only. No general workspace scan or delete API.
import {constants} from 'node:fs';
import {open,mkdir,lstat} from 'node:fs/promises';
import {createHash,randomUUID,timingSafeEqual} from 'node:crypto';
const SESSION=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const PROTECTED=new Set(['.agents','.dsh','.git','.maimi','skills','templates']);
const MAX_FILE=32*1024*1024,MAX_TOTAL=128*1024*1024;
const DIR=constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW;
const version=s=>`${s.dev}:${s.ino}:${s.size}:${s.mtimeNs}:${s.ctimeNs}`;
const fdpath=(handle,name)=>`/proc/self/fd/${handle.fd}/${name}`;
const sameDirectory=(a,b)=>a.isDirectory()&&b.isDirectory()&&a.dev===b.dev&&a.ino===b.ino;
function need(ok,message='output request invalid'){if(!ok)throw Error(message);}
function relative(value,directory){
 need(typeof value==='string'&&value.startsWith(directory+'/')&&value.length<=4096&&!/[\\\x00-\x1f\x7f]/.test(value));
 const parts=value.slice(directory.length+1).split('/');
 need(parts.every(part=>part&&part!=='.'&&part!=='..'&&!PROTECTED.has(part.toLowerCase())));
 return parts;
}
async function base(root){
 const workspace=await open(root,DIR);
 try{
  try{await mkdir(fdpath(workspace,'maimi-outputs'),{mode:0o700});}catch(error){if(error.code!=='EEXIST')throw error;}
  return await open(fdpath(workspace,'maimi-outputs'),DIR);
 }finally{await workspace.close();}
}
async function directoryHandle(root,entry){
 const parent=await base(root);
 try{
  const handle=await open(fdpath(parent,entry.name),DIR);
  try{need(sameDirectory(await handle.stat({bigint:true}),entry.identity),'output directory changed');return handle;}
  catch(error){await handle.close();throw error;}
 }finally{await parent.close();}
}
async function exactFile(root,entry,row,deadline){
 const pieces=relative(row.path,entry.directory),handles=[];
 try{
  let parent=await directoryHandle(root,entry);handles.push(parent);
  for(const piece of pieces.slice(0,-1)){parent=await open(fdpath(parent,piece),DIR);handles.push(parent);}
  const leaf=pieces.at(-1),file=await open(fdpath(parent,leaf),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);handles.push(file);
  const before=await file.stat({bigint:true});
  need(before.isFile()&&before.nlink===1n&&before.size===BigInt(row.bytes)&&version(before)===row.version,'output file changed');
  // Never readFile(): concurrent growth must not allocate beyond the admitted
  // size. One extra byte detects growth, including a previously empty file.
  const buffer=Buffer.alloc(row.bytes+1);let offset=0;
  while(offset<buffer.length){
   need(Date.now()<deadline,'output read deadline');
   const {bytesRead}=await file.read(buffer,offset,Math.min(1024*1024,buffer.length-offset),null);
   if(bytesRead===0)break;offset+=bytesRead;
  }
  const data=buffer.subarray(0,offset);
  need(data.length===row.bytes&&createHash('sha256').update(data).digest('hex')===row.sha256,'output file changed');
  const after=await file.stat({bigint:true}),bound=await lstat(fdpath(parent,leaf),{bigint:true});
  need(bound.isFile()&&bound.nlink===1n&&version(after)===row.version&&version(bound)===row.version,'output file changed');
  const rebound=await directoryHandle(root,entry);await rebound.close();
  return data;
 }finally{for(const handle of handles.reverse())await handle.close().catch(()=>{});}
}

export function createOutputBridge({token,owner,generation,modelBase,scopes,readJson,root='/workspace',fetcher=fetch,onAllocate=()=>{}}){
 need(/^[A-Za-z0-9_-]{43}$/.test(token)&&/^[a-f0-9]{32}$/.test(owner)&&Number.isSafeInteger(generation)&&generation>0);
 const entries=new Map();
 return async function handle(req,res){
  const auth=req.headers.authorization||'',expected='Bearer '+token;
  need(req.method==='POST'&&!req.headers.origin&&!req.headers['sec-fetch-site']&&Buffer.byteLength(auth)===Buffer.byteLength(expected)&&timingSafeEqual(Buffer.from(auth),Buffer.from(expected)),'output bridge denied');
  const session=req.headers['x-maimi-session-id'];need(SESSION.test(session||''));
  const operation=req.url.slice('/__megmeet/outputs/'.length);need(operation==='allocate'||operation==='publish');
  const body=await readJson(req,128*1024);need(body&&typeof body==='object'&&!Array.isArray(body));
  let entry=entries.get(session),answer;
  if(operation==='allocate'){
   need(Object.keys(body).length===0);
   const scope=await scopes.resolve(session);
   if(!entry){
    need(entries.size<256,'output directory limit');
    const parent=await base(root),name=session.replace(/^session-/,'')+'-'+randomUUID();
    try{
     // mkdir without recursive/adoption: an existing directory is never ours.
     await mkdir(fdpath(parent,name),{mode:0o700});
     const child=await open(fdpath(parent,name),DIR);
     try{entry={name,directory:'/workspace/maimi-outputs/'+name,identity:await child.stat({bigint:true}),lease:scope.lease,busy:false};}
     finally{await child.close();}
     entries.set(session,entry);
    }finally{await parent.close();}
   }else{const check=await directoryHandle(root,entry);await check.close();entry.lease=scope.lease;}
   onAllocate(session,entry.directory);answer={directory:entry.directory};
  }else{
   need(entry&&!entry.busy&&Object.keys(body).sort().join(',')==='directory,files'&&body.directory===entry.directory);
   need(Array.isArray(body.files)&&body.files.length<=32);
   let total=0;const seen=new Set();
   for(const row of body.files){
    need(row&&Object.keys(row).sort().join(',')==='bytes,name,path,sha256,version');
    relative(row.path,entry.directory);need(!seen.has(row.path));seen.add(row.path);
    need(typeof row.name==='string'&&row.name===row.path.split('/').at(-1)&&row.name.length<=255&&Number.isSafeInteger(row.bytes)&&row.bytes>=0&&row.bytes<=MAX_FILE);
    need(/^[a-f0-9]{64}$/.test(row.sha256||'')&&/^[0-9]{1,30}:[0-9]{1,30}:[0-9]{1,12}:-?[0-9]{1,30}:-?[0-9]{1,30}$/.test(row.version||''));
    total+=row.bytes;need(total<=MAX_TOTAL);
   }
   await scopes.validate({lease:entry.lease});entry.busy=true;answer={published:[],retained:[]};const deadline=Date.now()+60000;
   try{
    for(const row of body.files){
     try{
      need(Date.now()<deadline,'output read deadline');await scopes.validate({lease:entry.lease});const data=await exactFile(root,entry,row,deadline);await scopes.validate({lease:entry.lease});
      const response=await fetcher(new URL('/internal/storage/native/output',modelBase),{method:'POST',redirect:'error',signal:AbortSignal.timeout(Math.max(1,Math.min(30000,deadline-Date.now()))),headers:{
       Authorization:'Bearer '+token,'X-Maimi-Owner':owner,'X-Maimi-Generation':String(generation),'X-Maimi-Session-ID':session,
       'X-Maimi-Source-Path':Buffer.from(row.path).toString('base64url'),'X-Maimi-Source-Version':Buffer.from(row.version).toString('base64url'),
       'X-Maimi-SHA256':row.sha256,'Content-Type':'application/octet-stream','Content-Length':String(data.length)},body:data});
      const raw=await response.text();need(raw.length<=8192,'output backend unavailable');
      if(response.status!==201){answer.retained.push({path:row.path,reason:response.status===413||response.status===507?'CAPACITY_UNAVAILABLE':response.status===410?'SOURCE_EXPIRED':'BACKEND_UNAVAILABLE'});continue;}
      const receipt=JSON.parse(raw);need(receipt.status==='active'&&receipt.sha256===row.sha256&&receipt.expected_bytes===row.bytes,'output backend unavailable');
      answer.published.push({path:row.path,version:row.version});
     }catch(error){answer.retained.push({path:row.path,reason:error.message==='output file changed'||error.message==='output directory changed'?'SOURCE_CHANGED':'SOURCE_OR_BACKEND_UNAVAILABLE'});}
    }
   }finally{entry.busy=false;}
  }
  const bytes=Buffer.from(JSON.stringify(answer));res.writeHead(200,{'Content-Type':'application/json','Content-Length':String(bytes.length),'Cache-Control':'no-store'});res.end(bytes);
 };
}
