// Synthetic private-IPC fixture only. Not included in a company installer.
import {startNativeCompanyHost} from '../src/native-company-host.mjs';
let application;
process.once('message',async message=>{
  try {
    if(message.type!=='synthetic-start'||typeof message.capability!=='string')throw Error('FIXTURE_INIT_REQUIRED');
    process.env.DSH_HOME=message.home;
    process.env.MAIMI_LOCAL_MODEL_CAPABILITY=message.capability;
    application=await startNativeCompanyHost(message);
    const {ctx}=application;
    const url=ctx.connection.authenticatedUrl(`${ctx.webServer.protocol}//127.0.0.1:${ctx.webServer.port}`);
    process.send({type:'ready',url});
    const chunks=[];
    for await(const chunk of ctx.llm.stream({provider:'maimi-company',model:message.modelId,messages:[{role:'user',content:[{type:'text',text:'synthetic desktop test'}]}],maxTokens:32}))chunks.push(chunk);
    process.send({type:'model-result',chunks});
  } catch(error) {process.send?.({type:'fixture-error',message:String(error.message).slice(0,400)});process.exitCode=1;await application?.shutdown.shutdown(1);process.disconnect();}
});
process.on('message',async message=>{
  if(message.type==='shutdown'&&application){await application.shutdown.shutdown(0);process.send({type:'shutdown-complete'});process.disconnect();}
});
