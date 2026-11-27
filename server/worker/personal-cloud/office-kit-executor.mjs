// Fixed entry point in the employee's isolated execution Pod. No cloud credentials are passed.
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {constants} from 'node:fs';
import {open, lstat, realpath, mkdtemp, mkdir, readdir, rm, copyFile, writeFile} from 'node:fs/promises';
import {basename, dirname, join, extname} from 'node:path';
import {tmpdir} from 'node:os';
import {createHash} from 'node:crypto';

const require=createRequire('/opt/dsh/package.json');
const load=name=>import(pathToFileURL(require.resolve(name)).href);
const limit=50*1024**2;
const sid=/^(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i;
const workspace='/workspace';
const ownerTag=id=>createHash('sha256').update(id).digest('hex').slice(0,24);
const marker='maimi-office-kit-staging-v1';
const stop=new AbortController();
for(const signal of ['SIGTERM','SIGINT'])process.once(signal,()=>stop.abort());
const signal=AbortSignal.any([stop.signal,AbortSignal.timeout(110000)]);

function need(condition){if(!condition)throw Error('OFFICE_INPUT_INVALID');}
async function prune(root){
 const expiry=Date.now()-3600000;
 for(const dir of await readdir(root,{withFileTypes:true})){
  if(!dir.isDirectory()||!/^out-[0-7]$/.test(dir.name))continue;
  const path=join(root,dir.name),stamp=join(path,marker);
  const info=await lstat(stamp).catch(()=>null);
  if(info?.isFile()&&info.mtimeMs<expiry)await rm(path,{recursive:true,force:true});
 }
}
async function reserve(root){
 for(let i=0;i<8;i++){
  const path=join(root,'out-'+i);
  try{await mkdir(path,{mode:0o700});}
  catch(error){if(error.code==='EEXIST')continue;throw error;}
  try{await writeFile(join(path,marker),'1',{flag:'wx',mode:0o600});}
  catch(error){await rm(path,{recursive:true,force:true});throw error;}
  return path;
 }
 throw Object.assign(Error('Office staging is full.'),{code:'busy'});
}
async function privateDir(p){
 await mkdir(p,{mode:0o700}).catch(error=>{if(error.code!=='EEXIST')throw error;});
 need((await lstat(p)).isDirectory()&&await realpath(p)===p);
}
async function run(job){
 const {mode,session,path,version,extension,options}=job;
 need(sid.test(session)&&['renderImages','convertTo','recalculate','preview','cleanup'].includes(mode));
 const root=join(workspace,'.maimi-office-kit-'+ownerTag(session));
 if(mode==='cleanup'){
  need(typeof job.directory==='string'&&dirname(job.directory)===root&&/^out-[0-7]$/.test(basename(job.directory)));
  if(await realpath(root).catch(()=>null)===root&&await realpath(job.directory).catch(()=>null)===job.directory&&
      (await lstat(join(job.directory,marker)).catch(()=>null))?.isFile())await rm(job.directory,{recursive:true,force:true});
  return {done:true};
 }
 const inputStream=mode==='preview'&&job.fromStdin===true;
 if(inputStream)need(/^input\.(docx?|xlsx?|pptx?)$/.test(path)&&Number.isSafeInteger(job.bytes)&&job.bytes>0&&job.bytes<=limit);
 else need(typeof path==='string'&&path.startsWith(workspace+'/')&&await realpath(path)===path);
 need(extension===extname(path).slice(1).toLowerCase());
 const [{Context},{LocalFileSystem},{default:OfficeKit}]=await Promise.all([
  load('@deepseek-ai/cordis'),load('@deepseek-ai/dsh-fs-local'),load('@deepseek-ai/dsh-office-kit')]);
 const ctx=new Context();
 const scratch=await mkdtemp(join(tmpdir(),'maimi-office-engine-'));
 let output,committed=false;
 try{
  await ctx.plugin(LocalFileSystem,{cwd:workspace}).await();
  const target=inputStream?undefined:await ctx.fs.resolve(path,{cwd:workspace});
  async function check(){
   const info=await ctx.fs.stat(target,signal);
   need(ctx.fs.processPath(target)===path&&info?.type==='file'&&info.version===version&&Number.isSafeInteger(info.size)&&info.size>0&&info.size<=limit);
   return info;
  }
  const info=inputStream?{size:job.bytes}:await check();
  const source={key:JSON.stringify([session,path]),version,bytes:info.size,read:async(readSignal,maxBytes)=>{
   if(inputStream){
    const parts=[];let total=0;
    for await(const chunk of process.stdin){readSignal.throwIfAborted();total+=chunk.length;need(total<=info.size&&total<=maxBytes);parts.push(chunk);}
    need(total===info.size);return {bytes:Buffer.concat(parts,total),version};
   }
   await check();
   const handle=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW);
   try{
    const before=await handle.stat();need(before.isFile()&&before.size===info.size&&before.size<=maxBytes);
    const bytes=await handle.readFile({signal:readSignal});
    const after=await handle.stat();need(before.size===after.size&&before.mtimeMs===after.mtimeMs&&bytes.length===before.size);
    await check();return {bytes,version};
   }finally{await handle.close();}
  }};
  await ctx.plugin(OfficeKit,{outputDirectory:join(scratch,'outputs'),timeoutMs:100000,maxInputBytes:limit,
   maxOutputBytes:64*1024**2,maxSourceBytes:limit,maxOutputStoreBytes:64*1024**2,maxConcurrentConversions:1}).await();
  await privateDir(root);await prune(root);
  output=await reserve(root);
  const request={extension,source,...options};
  if(mode==='preview'){
   const result=await ctx.officeKit.convert({...request,priority:'foreground'},signal);
   need(result.pdf.byteLength>0&&result.pdf.byteLength<=32*1024**2);
   const to=join(output,'preview.pdf');await writeFile(to,result.pdf,{flag:'wx',mode:0o600});
   signal.throwIfAborted();committed=true;
   return {path:to,bytes:result.pdf.byteLength,sha256:createHash('sha256').update(result.pdf).digest('hex'),missingFonts:result.missingFonts};
  }
  if(mode==='renderImages'){
   const result=await ctx.officeKit.renderImages(request,signal);
   const images=[];
   for(const [index,image] of result.images.entries()){
    signal.throwIfAborted();const path=join(output,String(index)+'.png');
    await writeFile(path,image.png,{flag:'wx',mode:0o600});
    const {png,...metadata}=image;images.push({...metadata,path,bytes:png.byteLength,hash:createHash('sha256').update(png).digest('hex')});
   }
   committed=true;return {...result,images,directory:output};
  }
  need(['pdf','docx','odt','txt','xlsx','ods','csv','pptx','odp'].includes(options.format));
  const name=basename(path,extname(path))+'.'+options.format;
  const result=await ctx.officeKit[mode]({...request,output:{owner:session,name}},signal);
  const to=join(output,name);await copyFile(result.path,to,constants.COPYFILE_EXCL);
  signal.throwIfAborted();committed=true;return {...result,path:to};
 }finally{
  try{await ctx.fiber.dispose();}
  finally{await rm(scratch,{recursive:true,force:true});if(output&&!committed)await rm(output,{recursive:true,force:true});}
 }
}
try{const value=await run(JSON.parse(process.argv[2]));process.stdout.write(JSON.stringify({ok:true,value}));}
catch(e){const codes=new Set(['unavailable','invalid-request','invalid-output','timeout','input-too-large','output-too-large','source-changed','busy','unsupported-format','failed']);
 process.stdout.write(JSON.stringify({ok:false,code:codes.has(e?.code)?e.code:'failed'}));process.exitCode=1;}
