'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os');
function validateInputs(argv,environment){
  if(argv.length!==1||argv[0]!=='--self-check')throw Error('SELF_CHECK_ARGUMENTS');
  const directory=environment.MEGMEET_SELF_CHECK_USER_DATA;
  if(typeof directory!=='string'||!path.isAbsolute(directory))throw Error('SELF_CHECK_PRIVATE_DIRECTORY');
  const real=fs.realpathSync(directory),base=fs.realpathSync(os.tmpdir());
  if(path.dirname(real)!==base||!/^megmeet-self-check-[A-Za-z0-9-]{12,80}$/.test(path.basename(real))||!fs.statSync(real).isDirectory())throw Error('SELF_CHECK_PRIVATE_DIRECTORY');
  const pipe=environment.MEGMEET_SELF_CHECK_PIPE;
  if(pipe!==undefined&&!/^\\\\\.\\pipe\\megmeet-self-check-[a-f0-9-]{36}$/.test(pipe))throw Error('SELF_CHECK_PIPE');
  return {directory:real,pipe};
}
async function runSelfCheck(){
  const {app}=require('electron');let result,code=1,inputs;
  try{
    inputs=validateInputs(process.argv.slice(1),process.env);
    app.setPath('userData',inputs.directory);
    app.setPath('sessionData',inputs.directory);
    for(const flag of ['disable-background-networking','disable-component-update','disable-sync'])app.commandLine.appendSwitch(flag);
    await app.whenReady();
    if(!app.isPackaged)throw Error('SELF_CHECK_PACKAGED_REQUIRED');
    const runtime=path.join(process.resourcesPath,'runtime');
    const adapter=require('./dsh-adapter.cjs');
    const checked=adapter.validateRuntime(runtime,{full:true});
    if(typeof require('./account-lease.cjs').acquireAccountLease!=='function'||typeof require('./process-evidence-probe.cjs').createProcessEvidenceProbe!=='function')throw Error('SELF_CHECK_PROCESS_PROBE_MODULE');
    const codec=await import(require('node:url').pathToFileURL(path.join(runtime,'node_modules/@deepseek-ai/dsh-session-format-catalog/lib/index.js')));
    if(Object.keys(codec).length===0)throw Error('SELF_CHECK_NATIVE_MODULE');
    result={schema:'megmeet-installed-self-check-v1',status:'PASS',version:app.getVersion(),runtime:checked.manifest.dsh,executable:fs.realpathSync(process.execPath),nativeCodecLoaded:true,iamLoaded:false,credentialsRead:false,companyRequests:0};code=0;
  }catch(error){result={schema:'megmeet-installed-self-check-v1',status:'FAILED',code:/^SELF_CHECK_[A-Z_]+$/.test(error?.message)?error.message:'SELF_CHECK_INTEGRITY_OR_LOAD_FAILED',companyRequests:0};}
  try{
    const bytes=JSON.stringify(result)+'\n';
    if(inputs?.pipe){
      await new Promise((resolve,reject)=>{
        const socket=require('node:net').createConnection(inputs.pipe);socket.setTimeout(10000,()=>socket.destroy(Error('SELF_CHECK_PIPE_TIMEOUT')));
        socket.once('error',reject);socket.once('connect',()=>socket.end(bytes));socket.once('close',()=>resolve());
      });
    }else await new Promise((resolve,reject)=>{
      // A detached invocation may have no live console pipe. This is a
      // diagnostic transport failure, never an uncaught Electron exception.
      process.stdout.once('error',reject);
      process.stdout.write(bytes,error=>error?reject(error):resolve());
    });
  }catch{code=1;}
  app.exit(code);
}
module.exports={validateInputs,runSelfCheck};
