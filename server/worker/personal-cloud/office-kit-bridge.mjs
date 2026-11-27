import {AsyncLocalStorage} from 'node:async_hooks';
import {createHash, randomUUID} from 'node:crypto';
import {createRequire} from 'node:module';
import {posix} from 'node:path';
import {pathToFileURL} from 'node:url';
import {withAttachmentScope} from './native-attachment-plugin.mjs';
import {captureToolScope} from './native-task-context.mjs';

const require=createRequire('/opt/dsh/package.json');
const {OfficeKit,OfficeKitError}=await import(pathToFileURL(require.resolve('@deepseek-ai/dsh-office-kit')).href);
const toolMode=new Map([['office_render','renderImages'],['office_convert','convertTo'],['office_recalculate','recalculate']]);
const scopes=new AsyncLocalStorage();
const worker='/opt/personal-cloud/office-kit-executor.mjs';
const errors=new Set(['busy','invalid-request','invalid-output','input-too-large','output-too-large','source-changed','unsupported-format','unavailable','timeout']);
const allowedFormats=new Set(['pdf','docx','odt','txt','xlsx','ods','csv','pptx','odp']);
function need(test){if(!test)throw new OfficeKitError('invalid-request','The Office file or its session changed.');}

async function run(ctx,job,signal){
 signal?.throwIfAborted();
 const handle=ctx.subprocess.spawn({argv:[process.execPath,worker,JSON.stringify(job)],cwd:'/workspace',
  env:{LANG:'C.UTF-8',PATH:'/usr/local/bin:/usr/bin:/bin'},stdio:{stdin:'ignore',stdout:{maxBytes:65536},stderr:{maxBytes:2048}},graceMs:2000,signal});
 await handle.done;
 signal?.throwIfAborted();
 const reply=handle.collected.stdout.readFrom(0);
 if(reply.lossy)throw new OfficeKitError('invalid-output','Office result exceeded its metadata limit.');
 let data;
 try{data=JSON.parse(reply.text);}catch{throw new OfficeKitError('unavailable','The isolated Office engine did not return a result.');}
 if(data?.ok!==true)throw new OfficeKitError(errors.has(data?.code)?data.code:'failed','The isolated Office operation failed.');
 return data.value;
}

async function receiveImage(ctx,image,index,directory,limits,signal){
 need(image.path===posix.join(directory,index+'.png')&&/^[a-f0-9]{64}$/.test(image.hash));
 need(Number.isSafeInteger(image.bytes)&&image.bytes>=24&&image.bytes<=limits.maxBytes);
 need(Number.isSafeInteger(image.width)&&Number.isSafeInteger(image.height)&&image.width>0&&image.height>0&&
  Math.max(image.width,image.height)<=limits.maxDimension&&image.width*image.height<=limits.maxPixels);
 const target=await ctx.fs.resolve(image.path,{signal});
 const info=await ctx.fs.stat(target,signal);
 need(ctx.fs.processPath(target)===image.path&&info?.type==='file'&&info.size===image.bytes);
 const png=Buffer.allocUnsafe(image.bytes);
 for(let offset=0;offset<png.length;){
  const length=Math.min(256*1024,png.length-offset);
  const chunk=await ctx.fs.readByteRange(target,{offset,length},signal);need(chunk.length===length);
  png.set(chunk,offset);offset+=length;
 }
 need(png.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]))&&createHash('sha256').update(png).digest('hex')===image.hash);
 const {path,hash,bytes,...metadata}=image;
 return {...metadata,png};
}

async function delegate(ctx,mode,request,signal){
 const scope=scopes.getStore();need(scope?.mode===mode);
 const key=JSON.parse(request.source.key);need(Array.isArray(key)&&key.length===2&&key[0]===scope.session);
 const path=key[1];need(typeof path==='string'&&posix.normalize(path)===path&&path.startsWith('/workspace/')&&
  request.extension===posix.extname(path).slice(1).toLowerCase());
 need(typeof request.source.version==='string'&&request.source.version.length<=4096);
 if(request.output)need(request.output.owner===scope.root||request.output.owner===scope.session);
 const options={};
 const fields=mode==='renderImages'?['pages','sheet','range','dpi','maxImages','maxPixels','maxDimension','maxBytes']:['format','sheet'];
 for(const field of fields)if(request[field]!==undefined)options[field]=request[field];
 if(mode==='renderImages')options.maxBytes=Math.min(options.maxBytes,32*1024**2);
 else need(allowedFormats.has(options.format));
 const job={mode,session:scope.root,path,version:request.source.version,extension:request.extension,options};
 const result=await run(ctx,job,signal);
 const root='/workspace/.maimi-office-kit-'+createHash('sha256').update(scope.root).digest('hex').slice(0,24);
 const directory=mode==='renderImages'?result.directory:posix.dirname(result.path);
 need(posix.dirname(directory)===root&&/^out-[0-7]$/.test(posix.basename(directory)));
 if(mode!=='renderImages'){
  need(result.path===posix.join(directory,request.output.name)&&Number.isSafeInteger(result.bytes)&&result.bytes>0&&result.bytes<=64*1024**2);
  return result;
 }
 try{
  need(Array.isArray(result.images)&&result.images.length>0&&result.images.length<=options.maxImages);
  const total=result.images.reduce((sum,image)=>sum+image.bytes,0);need(Number.isSafeInteger(total)&&total<=options.maxBytes);
  const images=[];
  for(const [i,image] of result.images.entries())images.push(await receiveImage(ctx,image,i,directory,options,signal));
  const {directory:staging,...metadata}=result;return {...metadata,images};
 }finally{if(!signal?.aborted)await run(ctx,{mode:'cleanup',directory,session:scope.root},signal).catch(()=>{});}
}

export const name='maimi-isolated-office-kit';
export const inject=['fs','subprocess','agents'];
export function apply(ctx){
 ctx.on('tools/execute',(exec,next)=>{
  const mode=toolMode.get(exec.name);if(!mode)return next();
  const auth=captureToolScope(exec.agent);
  return scopes.run({mode,root:auth.rootSessionId,session:exec.agent.session.id},()=>
   withAttachmentScope({sessionId:auth.rootSessionId,requestId:randomUUID()},next));
 },{global:true,prepend:true});
 class IsolatedOfficeKit extends OfficeKit{
  constructor(inner,config){super(inner,config);this.outputDirectory='/workspace';}
  convert(){throw new OfficeKitError('unavailable','Open the document using its authorized workspace preview.');}
  renderImages(request,signal){return delegate(ctx,'renderImages',request,signal);}
  convertTo(request,signal){return delegate(ctx,'convertTo',request,signal);}
  recalculate(request,signal){return delegate(ctx,'recalculate',request,signal);}
 }
 ctx.plugin(IsolatedOfficeKit,{});
}
