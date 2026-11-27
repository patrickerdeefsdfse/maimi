import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import {createRequire,registerHooks} from 'node:module';
import { TARGET } from './native-build.mjs';
import {managedPluginProfile} from './managed-plugin-source/profile.mjs';
import * as wpsRead from './wps-read/skill.mjs';
import {prepareDesktopProfile} from './profile-state.mjs';
import * as desktopConnectors from './desktop-connectors.mjs';
import {fileURLToPath} from 'node:url';

/** Start the real upstream Desktop profile through its public profile-boot API.
 * This integration candidate owns no client bundle and no cloud authorization. */
export async function startNativeCompanyHost({ runtimeRoot, profileRoot, modelBase, modelId, models=[{id:modelId}], primaryRuntime, packageManager,enterprise,managedPlugins=true,managedPluginRoot }) {
  const base=new URL(modelBase);
  if(base.protocol!=='http:'||base.hostname!=='127.0.0.1'||!base.port||base.pathname!=='/v1'||base.search||base.hash||base.username||base.password)throw Error('LOOPBACK_MODEL_BRIDGE_REQUIRED');
  if(typeof modelId!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(modelId))throw Error('MODEL_ID_INVALID');
  for(const name of ['dsh','dsh-app-boot','dsh-base','dsh-web-app']){
    const file=path.join(runtimeRoot,'node_modules/@deepseek-ai',name,'package.json');
    if(JSON.parse(fs.readFileSync(file,'utf8')).version!==TARGET.version)throw Error('NATIVE_RUNTIME_VERSION_MISMATCH');
  }
  const connectors=desktopConnectors.desktopConnectorProfile({runtimeRoot,home:process.env.DSH_HOME});
  const patch=[
    {id:'system-prompt',disabled:true},
    {insert:[{id:'maimi-system-prompt',name:new URL('./company-system-prompt.mjs',import.meta.url).href,config:{personaPrefix:''}}]},
    {id:'llm-deepseek',disabled:true},{id:'llm-deepseek-account',disabled:true},
    {id:'ui-settings-account',disabled:true},{id:'account-controller',disabled:true},
    // Public client resources contain no grant or IAM credential. The host
    // counterpart is registered separately, solely with private IPC state.
    ...(enterprise?[{insert:[{id:'maimi-local-service-ui',name:new URL('./local-enterprise/service-ui/index.mjs',import.meta.url).href}]}]:[]),
    {id:'session-log-deepseek',config:{enabled:false,maxBytes:8*1024*1024}},
    {id:'session-telemetry-otel',config:{mode:'DISABLED'}},
    {id:'desktop-product-telemetry',disabled:true},{id:'product-analytics',disabled:true},
    {id:'llm-pi-ai',config:{providers:{'maimi-company':{displayName:'麦米公司模型',api:'openai-completions',baseURL:modelBase,apiKeyEnv:'MAIMI_LOCAL_MODEL_TOKEN',retryPolicy:{mode:'normal',maxRetries:0},models:models.map(({id,name,inputModalities})=>({id,name:name||id,...(inputModalities?{input:inputModalities}:{}),...(id==='deepseek-v4.1-flash'?{contextWindow:131072,maxTokens:8192,compat:{thinkingFormat:'deepseek'}}:{})}))}}}},
    {id:'agent-default-model',config:{provider:'maimi-company',model:modelId}},
    ...connectors.patch,
    ...(managedPlugins?managedPluginProfile({surface:'desktop',runtimeRoot,pluginRoot:managedPluginRoot,workspaceRoot:profileRoot}):[]),
  ];
  prepareDesktopProfile({profileRoot,bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app',...connectors.bundles],patch});
  const load=p=>import(pathToFileURL(path.join(runtimeRoot,'node_modules/@deepseek-ai',p)).href);
  if (primaryRuntime !== undefined) {
    if (!path.isAbsolute(primaryRuntime) || !fs.statSync(primaryRuntime).isDirectory()) throw Error('NATIVE_PRIMARY_RUNTIME_REQUIRED');
    process.env.DSH_PRIMARY_RUNTIME=primaryRuntime;
  }
  const {loadProfileDirectory,loadLayeredEnv}=await load('dsh-app-boot/lib/index.js');
  const {runProfile}=await load('dsh/lib/profile-boot.js');
  const installAnchor=path.join(runtimeRoot,'node_modules/@deepseek-ai/dsh/package.json');
  const profile=loadProfileDirectory('dsh',profileRoot,installAnchor);
  const application=await runProfile({environment:loadLayeredEnv('dsh',profileRoot),profile:'desktop',resolvedProfile:{profile,installAnchor},patchFiles:[],args:['--no-open','--host','127.0.0.1','--port','0'],...(packageManager===undefined?{}:{packageManager:{command:process.execPath,args:['--expose-internals',packageManager.pnpm],env:{ELECTRON_RUN_AS_NODE:'1',DSH_DESKTOP_NODE_EXECUTABLE:process.execPath,PATH:packageManager.nodeBin+path.delimiter+(process.env.PATH??'')}}})});
  if(enterprise){
    try{
      const pluginUrl=new URL('./local-enterprise/native-plugin.mjs',import.meta.url).href;
      const requireRuntime=createRequire(path.join(runtimeRoot,'package.json'));
      const hooks=registerHooks({resolve(specifier,context,next){
        if(specifier==='@deepseek-ai/dsh-mcp-client'&&context.parentURL===pluginUrl)return next(pathToFileURL(requireRuntime.resolve(specifier)).href,context);
        return next(specifier,context);
      }});
      let plugin;try{plugin=await import(pluginUrl);}finally{hooks.deregister();}
      await application.ctx.plugin(plugin,{endpoint:enterprise.endpoint,nativeCapability:enterprise.nativeCapability,getPolicy:()=>({revision:0,selected:[],remoteOffice:false}),enableServiceUi:true,serviceOnly:true});
    }catch(error){await application.shutdown.shutdown(1);throw error;}
  }
  if (primaryRuntime !== undefined) {
    try { await application.ctx.plugin(wpsRead,{root:path.join(process.env.DSH_HOME,'dsh-runtimes','maimi-wps-read'),node:path.join(primaryRuntime,'dependencies','node','bin',process.platform==='win32'?'node.exe':'node')}); }
    catch (error) { await application.shutdown.shutdown(1); throw error; }
  }
  try { await application.ctx.plugin(desktopConnectors,{root:fileURLToPath(new URL('./dws',import.meta.url))}); }
  catch(error) { await application.shutdown.shutdown(1); throw error; }
  return application;
}
