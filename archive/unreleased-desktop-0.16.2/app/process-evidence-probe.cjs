'use strict';
// One private PowerShell process per acquired account lease. Every request
// performs a fresh CIM scan; no successful quiescence evidence is cached.
const path=require('node:path');
const crypto=require('node:crypto');
const {spawn}=require('node:child_process');
const MAX_LINE=4096,MAX_QUEUE=32;
const SCRIPT=String.raw`
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$patch=$env:MAIMI_PROBE_ACCOUNT_PATCH
$runtime=$env:MAIMI_PROBE_RUNTIME_ENTRY
if(-not [IO.Path]::IsPathRooted($patch) -or -not [IO.Path]::IsPathRooted($runtime)){exit 2}
$expectedSequence=1
while($null -ne ($line=[Console]::In.ReadLine())) {
 try {
  if($line.Length -gt 256){throw 'invalid'}
  $request=$line|ConvertFrom-Json
  $names=@($request.PSObject.Properties.Name|Sort-Object)
  if(($names -join ',') -ne 'id,schema,sequence' -or $request.schema -ne 1 -or $request.id -notmatch '^[a-f0-9]{32}$' -or $request.sequence -ne $expectedSequence){throw 'invalid'}
  $unknown=0;$matching=0
  Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ForEach-Object {
   if([string]::IsNullOrEmpty($_.CommandLine)){$unknown++}
   elseif($_.CommandLine.IndexOf($patch,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine.IndexOf($runtime,[StringComparison]::OrdinalIgnoreCase) -ge 0){$matching++}
  }
  $value=[ordered]@{schema=1;id=$request.id;sequence=$expectedSequence;matching=$matching;unknown=$unknown}
  [Console]::Out.WriteLine(($value|ConvertTo-Json -Compress))
  [Console]::Out.Flush()
  $expectedSequence++
 } catch {
  [Console]::Out.WriteLine('{"schema":1,"failure":"PROBE_FAILED"}')
  [Console]::Out.Flush()
  exit 3
 }
}
`;
function refused(code){return Object.assign(new Error(code),{code});}
function createProcessEvidenceProbe(home,runtimeRoot,options={}) {
 const platform=options.platform??process.platform;
 const timeoutMs=options.timeoutMs??15000;
 if(platform!=='win32'||typeof home!=='string'||typeof runtimeRoot!=='string'||!path.win32.isAbsolute(home)||!path.win32.isAbsolute(runtimeRoot)
   ||!Number.isSafeInteger(timeoutMs)||timeoutMs<1||timeoutMs>15000)throw refused('DSH_PROCESS_PROBE_OPTIONS_INVALID');
 const environment=options.environment??process.env;
 const systemRoot=environment.SystemRoot||'C:\\Windows';
 if(!path.win32.isAbsolute(systemRoot))throw refused('DSH_PROCESS_PROBE_OPTIONS_INVALID');
 const shell=path.win32.join(systemRoot,'System32','WindowsPowerShell','v1.0','powershell.exe');
 const launch=options.spawn??spawn;
 let child;
 try {child=launch(shell,['-NoLogo','-NoProfile','-NonInteractive','-Command',SCRIPT],{
  windowsHide:true,shell:false,stdio:['pipe','pipe','pipe'],env:{SystemRoot:systemRoot,PATH:environment.PATH||'',
   MAIMI_PROBE_ACCOUNT_PATCH:path.win32.join(home,'desktop-profile.patch.json'),
   MAIMI_PROBE_RUNTIME_ENTRY:path.win32.join(runtimeRoot,'node_modules','@deepseek-ai','dsh','lib','bin.js')}});
 }catch{throw refused('DSH_PROCESS_PROBE_START_FAILED');}
 let terminal=null,active=null,sequence=0,buffer=Buffer.alloc(0),killed=false;
 const queue=[];
 const dispose=()=>{
  if(killed)return;killed=true;
  // Never use a process name, arbitrary PID, taskkill tree, or global sweep.
  try{child.stdin.destroy();}catch{}
  if(child.exitCode===null&&child.signalCode===null){try{child.kill();}catch{}}
 };
 const fail=code=>{
  if(terminal)return;terminal=refused(code);buffer=Buffer.alloc(0);
  if(active){clearTimeout(active.timer);active.reject(terminal);active=null;}
  for(const pending of queue.splice(0))pending.reject(terminal);
  dispose();
 };
 const pump=()=>{
  if(terminal||active||!queue.length)return;
  const pending=queue.shift(),id=crypto.randomBytes(16).toString('hex');
  if(++sequence>2147483647){queue.unshift(pending);fail('DSH_PROCESS_PROBE_PROTOCOL_INVALID');return;}
  active={...pending,id,sequence,timer:setTimeout(()=>fail('DSH_PROCESS_PROBE_TIMEOUT'),timeoutMs)};
  active.timer.unref?.();
  try{child.stdin.write(JSON.stringify({schema:1,id,sequence})+'\n',error=>{if(error)fail('DSH_PROCESS_PROBE_UNAVAILABLE');});}
  catch{fail('DSH_PROCESS_PROBE_UNAVAILABLE');}
 };
 child.stdout.on('data',chunk=>{
  if(terminal)return;
  buffer=Buffer.concat([buffer,Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk)]);
  if(buffer.length>MAX_LINE){fail('DSH_PROCESS_PROBE_PROTOCOL_INVALID');return;}
  const newline=buffer.indexOf(10);if(newline<0)return;
  // The owned worker emits exactly one newline-terminated response per query.
  // Refuse trailing output instead of treating it as the next fresh result.
  if(!active||newline!==buffer.length-1){fail('DSH_PROCESS_PROBE_PROTOCOL_INVALID');return;}
  const line=buffer.subarray(0,newline);buffer=Buffer.alloc(0);
  let value;
  try{value=JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(line));}
  catch{fail('DSH_PROCESS_PROBE_PROTOCOL_INVALID');return;}
  if(!value||Array.isArray(value)||Object.keys(value).sort().join(',')!=='id,matching,schema,sequence,unknown'
   ||value.schema!==1||value.id!==active.id||value.sequence!==active.sequence
   ||!Number.isSafeInteger(value.matching)||!Number.isSafeInteger(value.unknown)||value.matching<0||value.unknown<0
   ||value.matching>1000000||value.unknown>1000000){fail('DSH_PROCESS_PROBE_PROTOCOL_INVALID');return;}
  const current=active;active=null;clearTimeout(current.timer);
  current.resolve({matching:value.matching,unknown:value.unknown});queueMicrotask(pump);
 });
 child.stdout.on('error',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 child.stdin.on('error',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 child.stderr.on('data',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 child.stderr.on('error',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 child.on('error',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 child.on('exit',()=>fail('DSH_PROCESS_PROBE_UNAVAILABLE'));
 return {
  query:()=>{
   if(terminal)return Promise.reject(terminal);
   if(queue.length>=MAX_QUEUE){fail('DSH_PROCESS_PROBE_QUEUE_LIMIT');return Promise.reject(terminal);}
   const result=new Promise((resolve,reject)=>queue.push({resolve,reject}));pump();return result;
  },
  close:()=>fail('DSH_PROCESS_PROBE_CLOSED'),
 };
}
module.exports={createProcessEvidenceProbe};
