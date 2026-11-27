// Private, incoming-only control channel for an isolated task's ephemeral volume.
import {constants} from 'node:fs';
import {mkdir,open,opendir,link,unlink} from 'node:fs/promises';
import {createHash,randomUUID} from 'node:crypto';
import {pipeline} from 'node:stream/promises';
const DIR=constants.O_RDONLY|constants.O_DIRECTORY|constants.O_NOFOLLOW;
const READ=constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK;
const MAX=32*1024**2, TOTAL=128*1024**2;
const UUID='[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}';
const sessionRx=new RegExp('^(?:session-)?'+UUID+'$');
const check=v=>{if(!v)throw Error('TASK_FILE_INVALID');};
const at=(h,key)=>'/proc/self/fd/'+h.fd+'/'+key;
const safe=s=>typeof s==='string'&&s.length<=255&&s.length>0&&!/[/\\\x00-\x1f\x7f]/.test(s)&&s!=='.'&&s!=='..';
const version=s=>[s.dev,s.ino,s.size,s.mtimeNs,s.ctimeNs].join(':');
const json=(res,val)=>{res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(val));};
async function parse(req){
 const len=Number(req.headers['content-length']);check(Number.isInteger(len)&&len>0&&len<=128*1024);
 const parts=[];let count=0;for await(const b of req){count+=b.length;check(count<=len);parts.push(b);}check(count===len);
 return JSON.parse(Buffer.concat(parts).toString('utf8'));
}
async function dir(base,parts,create=false){
 let handle=await open(base,DIR);
 try{
  for(const part of parts){
   check(safe(part));
   if(create)try{await mkdir(at(handle,part),{mode:0o700});}catch(e){if(e.code!=='EEXIST')throw e;}
   const next=await open(at(handle,part),DIR);await handle.close();handle=next;
  }
  return handle;
 }catch(e){await handle.close();throw e;}
}
function descriptors(rows){
 check(Array.isArray(rows)&&rows.length<=32);let sum=0;
 return rows.map(row=>{
  check(safe(row.name)&&/^[a-f0-9]{64}$/.test(row.sha256)&&Number.isSafeInteger(row.bytes)&&row.bytes>=0&&row.bytes<=MAX&&Array.isArray(row.aliases)&&row.aliases.length<=4);
  check((sum+=row.bytes)<=TOTAL);
  const paths=[['maimi-inputs',row.sha256,row.name]];
  for(const path of row.aliases){
   const parts=path.split('/');
   check(parts.length===10&&parts[0]===''&&parts[1]==='workspace'&&parts[2]==='.maimi-cache'&&/^cache-[a-zA-Z0-9]{6}$/.test(parts[3])&&
    parts[4]==='attachments'&&parts[5]==='v1'&&parts[6]==='files'&&parts[7]===row.sha256.slice(0,2)&&parts[8]===row.sha256&&parts[9]===row.name);
   paths.push(parts.slice(2));
  }
  return {sha256:row.sha256,bytes:row.bytes,paths};
 });
}
async function digestOpen(file,size){
 const h=createHash('sha256'),buf=Buffer.alloc(65536);
 let total=0;while(total<=size){const {bytesRead}=await file.read(buf,0,Math.min(buf.length,size+1-total),total);if(!bytesRead)break;total+=bytesRead;check(total<=size);h.update(buf.subarray(0,bytesRead));}
 check(total===size);return h.digest('hex');
}
async function materialize(root,parts,data){
 const parent=await dir(root,parts.slice(0,-1),true),name=parts.at(-1),tmp='.import-'+randomUUID();
 let created=false;
 try{
  let h=await open(at(parent,tmp),'wx',0o600);created=true;
  try{await h.writeFile(data);}finally{await h.close();}
  try{await link(at(parent,tmp),at(parent,name));}
  catch(e){
   if(e.code!=='EEXIST')throw e;
   h=await open(at(parent,name),READ);try{const s=await h.stat();check(s.isFile()&&s.nlink===1&&s.size===data.length&&await digestOpen(h,s.size)===createHash('sha256').update(data).digest('hex'));}finally{await h.close();}
  }
 }finally{if(created)await unlink(at(parent,tmp)).catch(()=>{});await parent.close();}
}
export function taskFileService({root='/workspace',busy,identity}){
 let session=null,inputs=null,signature=null,output=null,outIdentity=null,active=false;
 const imported=new Set(),ack=new Map(),offered=new Map();
 async function outDir(){
  const h=await dir(root,output.split('/').slice(2));
  try{const s=await h.stat({bigint:true});check(s.dev+':'+s.ino===outIdentity);return h;}catch(e){await h.close();throw e;}
 }
 async function list(){
  if(!output)return [];
  let count=0,total=0;const results=[];
  async function walk(parent,prefix,depth){
   for await(const e of await opendir('/proc/self/fd/'+parent.fd)){
    check(++count<=128&&safe(e.name));
    if(e.name.startsWith('.'))continue;
    const rel=prefix+e.name;
    if(e.isDirectory()){
     check(depth<4);const child=await open(at(parent,e.name),DIR);try{await walk(child,rel+'/',depth+1);}finally{await child.close();}continue;
    }
    check(e.isFile());
    const f=await open(at(parent,e.name),READ);
    try{
     const s=await f.stat({bigint:true});const bytes=Number(s.size);check(s.isFile()&&s.nlink===1n&&bytes<=MAX&&(total+=bytes)<=TOTAL);
     const v=version(s),path=output+'/'+rel;
     if(ack.get(path)===v)continue;
     const known=offered.get(path);
     const hash=known?.version===v?known.sha256:await digestOpen(f,bytes);
     check(version(await f.stat({bigint:true}))===v);
     const row={path,name:e.name,bytes,version:v,sha256:hash};results.push(row);check(results.length<=32);
    }finally{await f.close();}
   }
  }
  const h=await outDir();try{await walk(h,'',0);}finally{await h.close();}
  offered.clear();for(const row of results)offered.set(row.path,row);
  return results;
 }
 async function fulfilled(){if(!session)return true;if(active||busy())return false;try{return !(await list()).length;}catch{return false;}}
 async function handle(req,res){
  check(!active);active=true;
  try{
   const op=req.headers['x-maimi-file-operation'],sid=req.headers['x-maimi-file-session'];check(sessionRx.test(sid||'')&&(!session||session===sid));
   check(req.headers['x-maimi-file-generation']===String(identity));
   if(op==='prepare'){
    const m=await parse(req),rows=descriptors(m.inputs),sig=JSON.stringify(rows);
    check(!signature||signature===sig);
    if(m.output_directory!==null){
     const prefix='/workspace/maimi-outputs/'+sid.replace(/^session-/,'')+'-';
     check(typeof m.output_directory==='string'&&m.output_directory.startsWith(prefix)&&new RegExp('^'+UUID+'$').test(m.output_directory.slice(prefix.length)));
     check(!output||output===m.output_directory);
     if(!output){const h=await dir(root,m.output_directory.split('/').slice(2),true);
      try{const s=await h.stat({bigint:true});outIdentity=s.dev+':'+s.ino;output=m.output_directory;}finally{await h.close();}}
    }
    session=sid;signature=sig;inputs=rows;return json(res,{missing:rows.map((_,i)=>i).filter(i=>!imported.has(i))});
   }
   check(session===sid);
   if(op==='put'){
    const index=Number(req.headers['x-maimi-file-index']);check(Number.isInteger(index)&&index>=0&&index<inputs.length);
    const row=inputs[index];check(req.headers['content-length']===String(row.bytes));const chunks=[];let n=0;
    for await(const b of req){n+=b.length;check(n<=row.bytes);chunks.push(b);}
    const data=Buffer.concat(chunks,n);check(n===row.bytes&&createHash('sha256').update(data).digest('hex')===row.sha256);
    for(const parts of row.paths)await materialize(root,parts,data);
    imported.add(index);return json(res,{saved:true});
   }
   if(op==='list'){await parse(req);return json(res,busy()?{running:true}:{files:await list()});}
   const descriptor=await parse(req),row=offered.get(descriptor.path);
   check(row&&row.version===descriptor.version&&row.sha256===descriptor.sha256);
   if(op==='ack'){ack.set(row.path,row.version);return json(res,{saved:true});}
   check(op==='read');const top=await outDir();let parent=top,h;
   try{
    const ps=row.path.slice(output.length+1).split('/');
    for(const part of ps.slice(0,-1)){const next=await open(at(parent,part),DIR);if(parent!==top)await parent.close();parent=next;}
    h=await open(at(parent,ps.at(-1)),READ);const s=await h.stat({bigint:true});check(s.isFile()&&s.nlink===1n&&version(s)===row.version);
    res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Length':row.bytes,'Cache-Control':'no-store'});
    if(row.bytes)await pipeline(h.createReadStream({autoClose:false,start:0,end:row.bytes-1}),res);else res.end();
   }finally{await h?.close();if(parent!==top)await parent.close();await top.close();}
  }finally{active=false;}
 }
 return {handle,fulfilled};
}
