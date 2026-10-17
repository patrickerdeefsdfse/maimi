'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {createProcessEvidenceProbe}=require('./process-evidence-probe.cjs');
const run=promisify(execFile);
const created=value=>typeof value==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{1,7}Z$/.test(value)&&Number.isFinite(Date.parse(value));
function electronSingleInstanceHeld(){
  try{return require('electron').app?.hasSingleInstanceLock()===true;}catch{return false;}
}
// This probes only Node process launch metadata for the exact owned desktop
// account patch or reviewed runtime. It never emits command lines or env.
const probeScript=String.raw`
$ErrorActionPreference='Stop'
$patch=$env:MAIMI_PROBE_ACCOUNT_PATCH
$runtime=$env:MAIMI_PROBE_RUNTIME_ENTRY
if(-not [IO.Path]::IsPathRooted($patch) -or -not [IO.Path]::IsPathRooted($runtime)){throw 'invalid'}
$unknown=0;$matching=0
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ForEach-Object {
  if([string]::IsNullOrEmpty($_.CommandLine)){$unknown++}
  elseif($_.CommandLine.IndexOf($patch,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or $_.CommandLine.IndexOf($runtime,[StringComparison]::OrdinalIgnoreCase) -ge 0){$matching++}
}
[ordered]@{schema=1;matching=$matching;unknown=$unknown}|ConvertTo-Json -Compress
`;
async function windowsProcessEvidence(home,runtimeRoot) {
  if(process.platform!=='win32')throw Error('DSH_DESKTOP_PLATFORM_UNSUPPORTED');
  const shell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  const {stdout}=await run(shell,['-NoLogo','-NoProfile','-NonInteractive','-Command',probeScript],{
    windowsHide:true,timeout:15000,maxBuffer:4096,encoding:'utf8',
    env:{SystemRoot:process.env.SystemRoot,PATH:process.env.PATH,
      MAIMI_PROBE_ACCOUNT_PATCH:path.join(home,'desktop-profile.patch.json'),
      MAIMI_PROBE_RUNTIME_ENTRY:path.join(runtimeRoot,'node_modules','@deepseek-ai','dsh','lib','bin.js')}});
  const value=JSON.parse(stdout.replace(/^\uFEFF/,''));
  if(value.schema!==1||!Number.isSafeInteger(value.matching)||!Number.isSafeInteger(value.unknown)||value.matching<0||value.unknown<0)throw Error('DSH_PROCESS_EVIDENCE_INVALID');
  return value;
}
async function windowsOwnerEvidence(pid) {
  if(process.platform!=='win32'||!Number.isSafeInteger(pid)||pid<1)throw Error('DSH_OWNER_EVIDENCE_INVALID');
  const shell=path.join(process.env.SystemRoot||'C:\\Windows','System32','WindowsPowerShell','v1.0','powershell.exe');
  const script=String.raw`$ErrorActionPreference='Stop'
$ownerPid=[int]$env:MAIMI_PROBE_OWNER_PID
$row=Get-CimInstance Win32_Process -Filter ('ProcessId='+$ownerPid)
if(-not $row){[ordered]@{schema=1;status='absent';pid=$ownerPid}|ConvertTo-Json -Compress}
elseif(-not $row.CreationDate){[ordered]@{schema=1;status='unknown';pid=$ownerPid}|ConvertTo-Json -Compress}
else{[ordered]@{schema=1;status='running';pid=$ownerPid;createdAt=$row.CreationDate.ToUniversalTime().ToString('o')}|ConvertTo-Json -Compress}`;
  const {stdout}=await run(shell,['-NoLogo','-NoProfile','-NonInteractive','-Command',script],{
    windowsHide:true,timeout:15000,maxBuffer:4096,encoding:'utf8',
    env:{SystemRoot:process.env.SystemRoot,PATH:process.env.PATH,MAIMI_PROBE_OWNER_PID:String(pid)}});
  const value=JSON.parse(stdout.replace(/^\uFEFF/,''));
  if(value.schema!==1||value.pid!==pid||!['running','absent','unknown'].includes(value.status)||value.status==='running'&&!created(value.createdAt))throw Error('DSH_OWNER_EVIDENCE_INVALID');
  return value;
}
async function acquireAccountLease(home,runtimeRoot,{processEvidence=windowsProcessEvidence,ownerEvidence=windowsOwnerEvidence,singleInstanceHeld=electronSingleInstanceHeld}={}) {
  if(!path.isAbsolute(home)||!path.isAbsolute(runtimeRoot)||fs.lstatSync(home).isSymbolicLink())throw Error('DSH_ACCOUNT_PATH_UNSAFE');
  // Reuse only the owned PowerShell process, never its evidence: each fence
  // performs a fresh CIM query and validates the account lease on both sides.
  const probe=processEvidence===windowsProcessEvidence?createProcessEvidenceProbe(home,runtimeRoot):null;
  if(probe)processEvidence=()=>probe.query();
  try {
  const lock=path.join(home,'.megmeet-desktop-runtime.lock');
  const token=crypto.randomUUID();
  const self=await ownerEvidence(process.pid);
  if(self.status!=='running'||self.pid!==process.pid||!created(self.createdAt))throw Error('DSH_CURRENT_OWNER_UNKNOWN');
  const checkNativeStopped=async()=>{
    const evidence=await processEvidence(home,runtimeRoot);
    if(evidence.matching!==0||evidence.unknown!==0)throw Error('DSH_OLD_RUNTIME_MUST_BE_CLOSED');
  };
  let recovered=false,nestedSnapshot;
  const nestedLock=path.join(home,'.history-maintenance.lock');
  if(fs.existsSync(lock)){
    if(singleInstanceHeld()!==true)throw Error('DSH_SINGLE_INSTANCE_REQUIRED_FOR_RECOVERY');
    const original=fs.lstatSync(lock);
    if(!original.isFile()||original.isSymbolicLink()||original.nlink!==1||original.size>4096)throw Error('DSH_ACCOUNT_LOCK_UNSAFE');
    const oldBytes=fs.readFileSync(lock,'utf8');
    if(fs.existsSync(nestedLock)){
      const info=fs.lstatSync(nestedLock);
      if(!info.isFile()||info.isSymbolicLink()||info.nlink!==1||info.size!==0||fs.readFileSync(nestedLock).length!==0)throw Error('DSH_NESTED_MIGRATION_LOCK_UNKNOWN');
      nestedSnapshot={ino:info.ino,dev:info.dev,mtimeMs:info.mtimeMs};
    }
    let previous;
    try{previous=JSON.parse(oldBytes);}catch{throw Error('DSH_ACCOUNT_OWNER_UNKNOWN');}
    if(previous.schema!==2||!Number.isSafeInteger(previous.pid)||previous.pid<1||!created(previous.createdAt)||typeof previous.token!=='string'||!/^[a-f0-9-]{36}$/.test(previous.token))throw Error('DSH_ACCOUNT_OWNER_UNKNOWN');
    const deadOwner=async()=>{
      const owner=await ownerEvidence(previous.pid);
      if(owner.pid!==previous.pid||owner.status==='unknown'||!['running','absent'].includes(owner.status))throw Error('DSH_ACCOUNT_OWNER_UNKNOWN');
      // A different creation identity at the same PID proves the recorded
      // owner is gone. Never terminate that unrelated replacement process.
      if(owner.status==='running'){
        if(!created(owner.createdAt))throw Error('DSH_ACCOUNT_OWNER_UNKNOWN');
        if(owner.createdAt===previous.createdAt)throw Error('DSH_ACCOUNT_RUNTIME_ALREADY_OWNED');
        if(Date.parse(owner.createdAt)<=Date.parse(previous.createdAt))throw Error('DSH_ACCOUNT_OWNER_UNKNOWN');
      }
    };
    await deadOwner();await checkNativeStopped();
    await deadOwner();await checkNativeStopped();
    if(singleInstanceHeld()!==true)throw Error('DSH_SINGLE_INSTANCE_REQUIRED_FOR_RECOVERY');
    const current=fs.lstatSync(lock);
    if(current.isSymbolicLink()||current.ino!==original.ino||current.dev!==original.dev||current.nlink!==1||fs.readFileSync(lock,'utf8')!==oldBytes)throw Error('DSH_ACCOUNT_LEASE_CHANGED');
    fs.unlinkSync(lock);recovered=true;
  }
  let fd;
  try{fd=fs.openSync(lock,'wx',0o600);}catch(error){if(error.code==='EEXIST')throw Error('DSH_ACCOUNT_RUNTIME_ALREADY_OWNED');throw error;}
  const bytes=JSON.stringify({schema:2,pid:process.pid,createdAt:self.createdAt,token})+'\n';
  fs.writeFileSync(fd,bytes);fs.fsyncSync(fd);
  const identity=fs.fstatSync(fd);
  let released=false,ownedChild;
  const assertOwned=()=>{
    if(released)throw Error('DSH_ACCOUNT_LEASE_RELEASED');
    const current=fs.lstatSync(lock);
    if(current.isSymbolicLink()||!current.isFile()||current.ino!==identity.ino||current.dev!==identity.dev||fs.readFileSync(lock,'utf8')!==bytes)throw Error('DSH_ACCOUNT_LEASE_CHANGED');
  };
  let recoveredMigrationLock=false;
  if(recovered){
    // Only an account lock whose recorded owner was proven dead authorizes
    // recovery of its empty Windows migration lock. Hold the new outer lock,
    // recheck native quiescence, and never touch migration journals/backups.
    await checkNativeStopped();assertOwned();
    if(singleInstanceHeld()!==true)throw Error('DSH_SINGLE_INSTANCE_REQUIRED_FOR_RECOVERY');
    if(nestedSnapshot){
      const current=fs.lstatSync(nestedLock);
      if(!current.isFile()||current.isSymbolicLink()||current.nlink!==1||current.size!==0||current.ino!==nestedSnapshot.ino||current.dev!==nestedSnapshot.dev||current.mtimeMs!==nestedSnapshot.mtimeMs||fs.readFileSync(nestedLock).length!==0)throw Error('DSH_NESTED_MIGRATION_LOCK_CHANGED');
      fs.unlinkSync(nestedLock);recoveredMigrationLock=true;
    }else if(fs.existsSync(nestedLock))throw Error('DSH_NESTED_MIGRATION_LOCK_CHANGED');
  }
  return {
    recovered,recoveredMigrationLock,
    setChild:child=>{assertOwned();if(ownedChild||!child?.pid)throw Error('DSH_CHILD_OWNERSHIP_INVALID');ownedChild=child;},
    hasChild:()=>!!ownedChild,
    assertQuiescent:async()=>{
      assertOwned();
      if(ownedChild&&ownedChild.exitCode===null&&ownedChild.signalCode===null)throw Error('DSH_OWNED_RUNTIME_STILL_RUNNING');
      const evidence=await processEvidence(home,runtimeRoot);
      assertOwned();
      if(evidence.matching!==0||evidence.unknown!==0)throw Error('DSH_OLD_RUNTIME_MUST_BE_CLOSED');
      return {stopped:true,exclusive:true};
    },
    release:()=>{try{assertOwned();fs.closeSync(fd);fs.unlinkSync(lock);released=true;}finally{probe?.close();}},
  };
  } catch(error) {probe?.close();throw error;}
}
module.exports={acquireAccountLease,windowsProcessEvidence,windowsOwnerEvidence,electronSingleInstanceHeld};
