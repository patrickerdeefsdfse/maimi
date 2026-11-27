import fs from 'node:fs';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath,pathToFileURL} from 'node:url';

export function parseArguments(args){
  const accepted=new Set(['file','start','count','max-chars','timeout-ms','sheet','columns']);const values={};
  for(let i=0;i<args.length;i+=2){const key=args[i]?.replace(/^--/u,'');if(!args[i]?.startsWith('--')||!accepted.has(key)||values[key]!==undefined||args[i+1]===undefined)throw Error('INVALID_ARGUMENTS');values[key]=args[i+1];}
  const bounded=(key,fallback,min,max)=>{const n=values[key]===undefined?fallback:Number(values[key]);if(!Number.isSafeInteger(n)||n<min||n>max)throw Error('INVALID_'+key.toUpperCase().replaceAll('-','_'));return n;};
  if(typeof values.file!=='string'||!path.win32.isAbsolute(values.file)||values.file.startsWith('\\\\')||/[\x00-\x1f]/u.test(values.file)||values.file.slice(2).includes(':'))throw Error('LOCAL_ABSOLUTE_FILE_REQUIRED');
  if(!/^\.[a-z0-9]+$/iu.test(path.win32.extname(values.file))||!['.doc','.docx','.wps','.xls','.xlsx','.et','.ppt','.pptx','.dps','.pdf'].includes(path.win32.extname(values.file).toLowerCase()))throw Error('UNSUPPORTED_FILE_TYPE');
  return {file:values.file,start:bounded('start',1,1,1000000),count:bounded('count',20,1,100),maxChars:bounded('max-chars',40000,100,100000),timeoutMs:bounded('timeout-ms',45000,1000,60000),sheet:bounded('sheet',1,1,256),columns:bounded('columns',30,1,100)};
}
export function runReader(request,{spawnProcess=spawn,platform=process.platform,script=fs.readFileSync(new URL('./read-wps.ps1',import.meta.url),'utf8')}={}){
  if(platform!=='win32')return Promise.resolve({status:'UNAVAILABLE',error:'WINDOWS_WPS_REQUIRED',text:'',plaintext_files_created:0});
  const systemRoot=process.env.SystemRoot??'C:\\Windows';
  const executable=path.win32.join(systemRoot,'System32','WindowsPowerShell','v1.0','powershell.exe');
  return new Promise(resolve=>{
    let stdout='',stderrBytes=0,done=false,timedOut=false,overflow=false;
    const bootstrap="[Console]::InputEncoding=New-Object System.Text.UTF8Encoding($false);$p=[Console]::In.ReadToEnd()|ConvertFrom-Json;& ([ScriptBlock]::Create([string]$p.script)) ([string]$p.request)";
    const child=spawnProcess(executable,['-NoLogo','-NoProfile','-NonInteractive','-STA','-EncodedCommand',Buffer.from(bootstrap,'utf16le').toString('base64')],{windowsHide:true,stdio:['pipe','pipe','pipe'],env:{...process.env,PSModulePath:undefined}});
    const finish=value=>{if(done)return;done=true;clearTimeout(timer);resolve(value);};
    const failure=error=>({schema:'maimi-wps-read/v1',status:'FAILED',error,text:'',plaintext_files_created:0,existing_wps_closed:false,cleanup_confirmed:false});
    const timer=setTimeout(()=>{timedOut=true;child.kill();finish(failure('WPS_TIMEOUT_CLEANUP_UNCONFIRMED'));},request.timeoutMs);
    child.stdout.setEncoding('utf8');child.stdout.on('data',chunk=>{stdout+=chunk;if(Buffer.byteLength(stdout)>1500000){overflow=true;child.kill();finish(failure('OUTPUT_LIMIT_CLEANUP_UNCONFIRMED'));}});
    child.stderr.on('data',chunk=>{stderrBytes+=chunk.length;if(stderrBytes>64000){overflow=true;child.kill();finish(failure('ERROR_OUTPUT_LIMIT'));}});
    child.once('error',()=>finish(failure('HELPER_START_FAILED')));
    child.once('close',code=>{if(timedOut||overflow)return;try{const result=JSON.parse(stdout.trim().replace(/^\uFEFF/u,''));if(code!==0||result.schema!=='maimi-wps-read/v1'||typeof result.text!=='string'||result.text.length>request.maxChars||typeof result.cleanup_confirmed!=='boolean')throw Error();finish(result);}catch{finish(failure('HELPER_RESULT_INVALID'));}});
    child.stdin.on('error',()=>{});child.stdin.end(JSON.stringify({script,request:JSON.stringify(request)}));
  });
}
if(process.argv[1]&&pathToFileURL(path.resolve(process.argv[1])).href===import.meta.url){
  try{const result=await runReader(parseArguments(process.argv.slice(2)));process.stdout.write(JSON.stringify(result)+'\n');process.exitCode=result.status==='READ'?0:2;}
  catch(error){process.stdout.write(JSON.stringify({schema:'maimi-wps-read/v1',status:'REFUSED',error:String(error.message),text:'',plaintext_files_created:0,existing_wps_closed:false,cleanup_confirmed:true})+'\n');process.exitCode=2;}
}
