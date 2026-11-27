// C-only execution capability boundary. Never loads a model provider or chat history.
import {createHash} from 'node:crypto';
import {posix} from 'node:path';
const UUID=/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const SESSION=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;
const MAX=8*1024*1024, MAX_JOBS=16, MAX_RECORDS=2048;
function boundedRead(reader,offset){
 const value=reader.readFrom(offset),bytes=Buffer.from(value.text),start=value.nextOffset-bytes.length;
 let size=Math.min(bytes.length,65536);while(size<bytes.length&&size>0&&(bytes[size]&0xc0)===0x80)size--;
 return {...value,text:bytes.subarray(0,size).toString('utf8'),nextOffset:start+size};
}
function need(ok,code='EXECUTION_REQUEST_INVALID'){if(!ok)throw Object.assign(Error(code),{code});}
export function safePath(value,{write=false}={}){
 need(typeof value==='string'&&value.length<=4096&&!/[\x00-\x1f\\]/.test(value));
 const path=posix.resolve('/workspace',value);
 need(path==='/workspace'||path.startsWith('/workspace/')||!write&&(path==='/opt/dsh'||path.startsWith('/opt/dsh/')||path==='/tmp'||path.startsWith('/tmp/')),'EXECUTION_PATH_DENIED');
 return path;
}
export class ExecutionRPC {
 constructor({fs,subprocess,now=Date.now,readDocument}){this.fs=fs;this.subprocess=subprocess;this.now=now;this.readDocument=readDocument;this.records=new Map();this.jobs=new Map();this.inflight=0;this.timer=setInterval(()=>{void this.sweep();},5000);this.timer.unref();}
 async sweep(){
  for(const [id,job]of this.jobs){
   if(!job.empty&&this.now()-job.seen>30000)job.handle.terminate();
   if(job.finished&&!job.empty&&!job.checking){job.checking=true;try{job.empty=await job.handle.waitForExit(AbortSignal.timeout(1000));}catch{job.empty=false;}finally{job.checking=false;}}
   if(job.empty&&this.now()-job.seen>60000)this.jobs.delete(id);
  }
 }
 busy(){return this.inflight>0||[...this.jobs.values()].some(j=>!j.empty);}
 async dispatch(body){
  need(body&&Object.keys(body).sort().join(',')==='args,operation,request_id,session_id');
  need(UUID.test(body.request_id)&&SESSION.test(body.session_id)&&typeof body.operation==='string'&&body.args&&typeof body.args==='object');
  const digest=createHash('sha256').update(JSON.stringify(body)).digest('hex'),old=this.records.get(body.request_id);
  if(old){need(old.digest===digest,'EXECUTION_REQUEST_ID_REUSED');return old.promise;}
  if(['fs.resolve','fs.lstat','fs.stat','fs.readText','fs.readBytes','fs.readByteRange','fs.listDir','process.environment','process.resolve','process.poll'].includes(body.operation)){
   this.inflight++;try{return await this.perform(body.operation,body.args,body.session_id);}finally{this.inflight--;}
  }
  // IDs are never silently forgotten and replayed within this generation.
  need(this.records.size<MAX_RECORDS,'EXECUTION_GENERATION_REQUEST_LIMIT');
  const record={digest,promise:null};this.records.set(body.request_id,record);this.inflight++;
  record.promise=this.perform(body.operation,body.args,body.session_id).finally(()=>{this.inflight--;});
  return record.promise;
 }
 async target(value,write=false){
  need(value&&typeof value.displayPath==='string');
  const target=await this.fs.resolve(safePath(value.displayPath,{write}));
  safePath(this.fs.processPath(target),{write});return target;
 }
 async perform(operation,args,session){
  const f=this.fs;
  if(operation==='fs.resolve'){const target=await f.resolve(safePath(args.path),{cwd:'/workspace'});safePath(f.processPath(target));return target;}
  if(operation==='fs.lstat')return await f.lstat(safePath(args.path),{cwd:'/workspace'})??null;
  if(operation.startsWith('fs.')){
   const write=['fs.writeText','fs.editText'].includes(operation),t=await this.target(args.target,write);
   if(operation==='fs.stat')return await f.stat(t)??null;
   if(operation==='fs.listDir'){const rows=await f.listDir(t);need(rows.length<=10000,'EXECUTION_DIRECTORY_TOO_LARGE');return rows;}
   if(operation==='fs.readText'){const s=await f.stat(t);need(s?.type==='file'&&s.size<=MAX,'EXECUTION_FILE_TOO_LARGE');return f.readText(t);}
   if(operation==='fs.readBytes'){need(Number.isSafeInteger(args.maxBytes)&&args.maxBytes>=0&&args.maxBytes<=MAX);return {base64:Buffer.from(await f.readBytes(t,undefined,args.maxBytes)).toString('base64')};}
   if(operation==='fs.readByteRange'){need(Number.isSafeInteger(args.offset)&&args.offset>=0&&Number.isSafeInteger(args.length)&&args.length>=0&&args.length<=MAX);return {base64:Buffer.from(await f.readByteRange(t,{offset:args.offset,length:args.length})).toString('base64')};}
   if(operation==='fs.writeText'){need(typeof args.content==='string'&&Buffer.byteLength(args.content)<=MAX);return f.writeText(t,args.content,args.expected);}
   if(operation==='fs.editText'){need(Buffer.byteLength(JSON.stringify(args.edit))<=MAX);return f.editText(t,args.edit,args.expected);}
  }
  if(operation==='process.environment')return this.subprocess.terminalEnvironment();
  if(operation==='process.resolve'){need(typeof args.command==='string'&&args.command.length<=4096);return this.subprocess.resolveExecutable(args.command);}
  if(operation==='document.read'){need(this.readDocument,'DOCUMENT_READER_UNAVAILABLE');safePath(args.file_path);return this.readDocument(args,{sessionId:session});}
  if(operation==='process.spawn'){
   need(this.jobs.size<MAX_JOBS,'EXECUTION_PROCESS_LIMIT');
   const spec=args.spec;need(spec&&Array.isArray(spec.argv)&&spec.argv.length>0&&spec.argv.length<=256&&spec.argv.every(x=>typeof x==='string'&&x.length<=MAX));
   need(spec.stdio&&!spec.stdio.control&&['ignore','pipe'].includes(spec.stdio.stdin)||spec?.stdio&&typeof spec.stdio.stdin?.data==='string');
   need(!spec.stdio.control,'EXECUTION_CONTROL_PIPE_UNSUPPORTED');
   const modes={};for(const stream of ['stdout','stderr']){
    const mode=spec.stdio[stream];need(mode==='pipe'||mode==='inherit'||mode&&Number.isSafeInteger(mode.maxBytes)&&mode.maxBytes>0&&mode.maxBytes<=MAX);
    modes[stream]=mode==='pipe'?'pipe':mode==='inherit'?{maxBytes:65536}: {maxBytes:mode.maxBytes,...(mode.spill?{spill:{maxBytes:Math.min(MAX,mode.spill.maxBytes)}}:{})};
   }
   const id=args.job_id;need(UUID.test(id)&&!this.jobs.has(id));
   need(Number.isFinite(spec.graceMs)&&spec.graceMs>0&&spec.graceMs<=10000);
   const env={};for(const [k,v]of Object.entries(spec.env||{})){need(/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(k)&&typeof v==='string'&&v.length<=16384);need(!/(?:TOKEN|SECRET|PASSWORD|KEY)|^(?:NODE_OPTIONS|LD_|CLOUD_|MAIMI_)/i.test(k),'EXECUTION_ENV_DENIED');env[k]=v;}
   let cwd=safePath(spec.cwd,{write:true});
   let argv=spec.argv;
   if(argv[0]==='/opt/dsh/node_modules/@vscode/ripgrep-linux-x64/bin/rg'){
    // Native glob/grep take at most one explicit search root after "--".
    // Resolve it on the executor that owns the files, including symlinks.
    const inWorkspace=async path=>safePath(f.processPath(await f.resolve(path)),{write:true});
    cwd=await inWorkspace(cwd);
    const separator=argv.indexOf('--');
    if(separator>=0){
     need(separator===argv.length-2,'EXECUTION_REQUEST_INVALID');
     const searchRoot=safePath(posix.resolve(cwd,argv[separator+1]),{write:true});
     argv=[...argv.slice(0,separator+1),await inWorkspace(searchRoot)];
    }
   }
   const handle=this.subprocess.spawn({...spec,argv,cwd,env,stdio:{stdin:spec.stdio.stdin, ...modes},signal:undefined});
   const job={handle,session,finished:false,empty:false,error:false,outcome:null,created:this.now(),seen:this.now(),raw:{}};this.jobs.set(id,job);
   for(const stream of ['stdout','stderr'])if(modes[stream]==='pipe'){
    const item={bytes:Buffer.alloc(0),total:0};job.raw[stream]=item;
    handle[stream].on('data',b=>{item.total+=b.length;item.bytes=Buffer.concat([item.bytes,b]).subarray(-1024*1024);});
   }
   handle.done.then(v=>{job.outcome=v;job.finished=true;},()=>{job.error=true;job.finished=true;}).then(async()=>{try{job.empty=await handle.waitForExit(AbortSignal.timeout(10000));}catch{job.empty=false;}});
   return {job_id:id};
  }
  if(operation.startsWith('process.')){
   const job=this.jobs.get(args.job_id);need(job&&job.session===session,'EXECUTION_PROCESS_NOT_OWNED');
   job.seen=this.now();
   if(operation==='process.poll'){
    const output={};let pending=false;
    for(const stream of ['stdout','stderr']){
     const offset=args.offsets?.[stream]??0;need(Number.isSafeInteger(offset)&&offset>=0);
     if(job.raw[stream]){
      const item=job.raw[stream],start=item.total-item.bytes.length,from=Math.max(offset,start),bytes=item.bytes.subarray(from-start,from-start+65536);
      output[stream]={base64:bytes.toString('base64'),nextOffset:from+bytes.length,lossy:offset<start};pending ||= output[stream].nextOffset<item.total;
     }else if(job.handle.collected[stream]){
      output[stream]=boundedRead(job.handle.collected[stream],offset);pending ||= output[stream].nextOffset<job.handle.collected[stream].readFrom(output[stream].nextOffset).nextOffset;
     }
    }
    return {finished:job.finished&&!pending,empty:job.empty,error:job.error,outcome:job.outcome,...output};
   }
   if(operation==='process.terminate'){job.handle.terminate();return {requested:true};}
   if(operation==='process.stdin'){need(job.handle.stdin&&typeof args.data==='string'&&Buffer.byteLength(args.data)<=90000);const bytes=Buffer.from(args.data,'base64');need(bytes.length<=65536&&bytes.toString('base64')===args.data);await new Promise((resolve,reject)=>job.handle.stdin.write(bytes,e=>e?reject(e):resolve()));if(args.end)job.handle.stdin.end();return {accepted:true};}
   if(operation==='process.forget'){need(job.empty,'EXECUTION_STOP_UNCONFIRMED');this.jobs.delete(args.job_id);return {forgotten:true};}
  }
  throw Object.assign(Error('EXECUTION_OPERATION_UNSUPPORTED'),{code:'EXECUTION_OPERATION_UNSUPPORTED'});
 }
 async close(){clearInterval(this.timer);for(const job of this.jobs.values())job.handle.terminate();await Promise.allSettled([...this.jobs.values()].map(j=>j.handle.waitForExit(AbortSignal.timeout(10000))));}
}
