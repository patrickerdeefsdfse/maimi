// Native desktop private IPC entry. Company credentials never enter this process.
import {startNativeCompanyHost} from './native-company-host.mjs';
import {installDesktopQuitInspection} from './upstream-native/quit-inspection.ts';
import {installDesktopUpdateTaskControl} from './upstream-native/update-tasks.ts';
let application,stopping;
let quitInspection,updateTasks;
const send=message=>new Promise(resolve=>process.connected?process.send(message,resolve):resolve());
const stop=()=>stopping??=(async()=>{
 await application?.shutdown.shutdown(0);await send({type:'shutdown-complete'});
 if(process.connected)process.disconnect();
})();
process.once('message',async message=>{
 try {
  if(message.type!=='maimi-native-start'||typeof message.capability!=='string')throw Error('PRIVATE_IPC_INIT_REQUIRED');
  process.env.DSH_HOME=message.home;
  // TOKEN deliberately matches the native subprocess credential scrub policy.
  process.env.MAIMI_LOCAL_MODEL_TOKEN=message.capability;
  application=await startNativeCompanyHost(message);
  if(stopping){await application.shutdown.shutdown(0);return;}
  const {ctx}=application;
  quitInspection=installDesktopQuitInspection(ctx);
  updateTasks=installDesktopUpdateTaskControl(ctx);
  await send({type:'ready',url:ctx.connection.authenticatedUrl(`${ctx.webServer.protocol}//127.0.0.1:${ctx.webServer.port}`),injections:ctx.webServer.collectIndexInjections()});
 } catch {
  await send({type:'fatal',message:'COMPANY_NATIVE_HOST_START_FAILED'});process.exitCode=1;
  await stop();
 }
});
process.on('message',message=>{if(message?.type==='shutdown')void stop();});
process.on('message',async message=>{
 if(!Number.isSafeInteger(message?.requestId))return;
 try{
  if(message.type==='quit-inspection'){
   if(stopping||!quitInspection)throw Error();
   await send({type:message.type,requestId:message.requestId,...await quitInspection()});
  }
  if(message.type==='update-tasks'&&['inspect','lock','unlock'].includes(message.action)){
   if(stopping||!updateTasks)throw Error();
   await send({type:message.type,requestId:message.requestId,active:await updateTasks(message.action)});
  }
 }catch{await send({type:message.type,requestId:message.requestId,active:true,activeTasks:true,scheduledTasks:true,error:'NATIVE_TASK_STATE_UNAVAILABLE'});}
});
process.once('disconnect',()=>{void stop();});
// The parent waits for handlers to exist before sending private startup data.
await send({type:'bootstrap-ready'});
