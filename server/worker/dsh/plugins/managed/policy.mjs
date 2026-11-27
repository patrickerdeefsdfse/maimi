import {randomUUID} from 'node:crypto';
/** Company policy is host configuration, never a browser preference. */
export const CONTEXT_ROUTES = Object.freeze(['detail','fleet','skills','backfill','attachment','balance'].map(x=>'/api/dsh-context/'+x));
export const VERSIONS = Object.freeze({dsh:'0.2.1-rc.3',context:'0.77.0',sidebar:'0.28.0',market:'1.70.2',modlens:'3.27.2',modsearch:'5.10.10',files:'0.6.2',genui:'0.14.0',browser:'0.3.3'});
export const PLUGIN_MANAGER_BLOCKED_METHODS=Object.freeze(['setVersionExemption','setPluginEnabled','setBundleEnabled','installBundle','cancelInstall','removeBundle','inspect']);
export function installPluginManagerFence(ctx) {
 ctx.inject(['pluginManager'],scope=>{
  const service=scope.pluginManager;
  for(const method of PLUGIN_MANAGER_BLOCKED_METHODS){
   if(typeof service[method]!=='function')throw Error('REVIEWED_PLUGIN_MANAGER_API_CHANGED');
   const descriptor=Object.getOwnPropertyDescriptor(service,method);
   const denied=()=>{throw Error('PLUGIN_MANAGEMENT_DISABLED_BY_COMPANY');};
   Object.defineProperty(service,method,{configurable:true,writable:false,value:denied});
   scope.effect(()=>()=>{if(service[method]===denied){if(descriptor)Object.defineProperty(service,method,descriptor);else delete service[method];}},'maimi: deny plugin manager '+method);
  }
 });
}
export const CATALOG = Object.freeze([
 {name:'dsh-files',version:VERSIONS.files,surfaces:['web','desktop'],state:'managed',purpose:'分页读取文档',restrictions:['固定作者 Git 源码；未使用同名 npm 占位包','网页版解析仅在按需执行环境','桌面办公文件和企业 PDF 优先 wps-read','不加载独立附件库/删除/导出副本']},
 {name:'@changfenhuang/dsh-genui',version:VERSIONS.genui,surfaces:['web','desktop'],state:'managed',purpose:'交互图表、表格和流程图',restrictions:['界面表单状态仅内存，聊天仍遵循历史清理','无外部媒体自动加载','不自动反复请求模型修复']},
 {name:'maimi-appearance',version:'0.0.0-r73',surfaces:['web','desktop'],state:'managed',purpose:'轻量配色',restrictions:['只保存本机配色选项','不接入动态视频和背景文件存储']},
 {name:'@wxg-prc-cpg/browser-skill-dsh-plugin',version:VERSIONS.browser,surfaces:['desktop'],state:'installed-not-configured',purpose:'本机浏览器操作',restrictions:['需本机程序与扩展完成配对','未配对不注册浏览器执行工具','不加载到网页版']},
 {name:'dsh-context',version:VERSIONS.context,surfaces:['web','desktop'],state:'managed',purpose:'上下文与工具使用统计',restrictions:['公司模型费用不使用公共价格估算','不向全局提示词注入前后缀','附件复用原文件']},
 {name:'dsh-better-sidebar',version:VERSIONS.sidebar,surfaces:['desktop'],state:'managed',purpose:'文件、Git与预览',restrictions:['仅本机用户权限','额外模型生成与任务修改需公司任务授权']},
 {name:'dshmarket',version:VERSIONS.market,surfaces:[],state:'catalog-only',purpose:'插件目录参考',restrictions:['不加载安装、自动修复、备份及凭据上传功能','由发布构建固定安装']},
 {name:'@liustack/modlens',version:VERSIONS.modlens,surfaces:['web','desktop'],state:'optional-not-enabled',purpose:'图像识别',restrictions:['需公司视觉模型与文件外发授权','不接受员工自填外部服务密钥']},
 {name:'@liustack/modsearch',version:VERSIONS.modsearch,surfaces:['web','desktop'],state:'optional-not-enabled',purpose:'网页检索',restrictions:['需公司批准的搜索出口与查询外发策略','不接受员工自填外部服务密钥']},
 {name:'@deepseek-ai/dsh-experimental-session-search',version:VERSIONS.dsh,surfaces:['web','desktop'],state:'optional',purpose:'个人会话检索',restrictions:['仅当前员工历史']},
 {name:'@deepseek-ai/dsh-skill-office',version:VERSIONS.dsh,surfaces:['web','desktop'],state:'native',purpose:'原生 Office 技能',restrictions:[]},
]);

export function surfaceOf(config={}) {
 if(!['web','desktop'].includes(config.surface))throw Error('MANAGED_PLUGIN_SURFACE_REQUIRED');
 return config.surface;
}
export function publicCatalog(surface){surfaceOf({surface});return {schema:'maimi-r73-managed-plugin-catalog/v1',runtime:VERSIONS.dsh,dynamic_installation:false,plugins:CATALOG.map(x=>({...x,available:x.surfaces.includes(surface)}))};}

/** Reject foreign session IDs before the upstream custom route can read a log.
 * The cloud gateway additionally binds the employee and active client lease.
 * A separate DSH host and data directory per employee remain mandatory. */
export async function validateContextRequest(request, ctx, config) {
 const pathname=new URL(request.url).pathname;
 if(request.method!=='POST'||!CONTEXT_ROUTES.includes(pathname))throw Error('PLUGIN_ROUTE_DENIED');
 const text=await request.clone().text();
 if(Buffer.byteLength(text)>16384)throw Error('PLUGIN_REQUEST_TOO_LARGE');
 let body;try{body=text?JSON.parse(text):{};}catch{throw Error('PLUGIN_BODY_INVALID');}
 if(!body||typeof body!=='object'||Array.isArray(body))throw Error('PLUGIN_BODY_INVALID');
 const route=pathname.split('/').at(-1);
 if(['detail','fleet','attachment'].includes(route)&&!(typeof body.sessionId==='string'&&body.sessionId.length>0&&body.sessionId.length<=200))throw Error('PLUGIN_SESSION_REQUIRED');
 if(body.sessionId!==undefined){
  if(typeof body.sessionId!=='string'||!body.sessionId||body.sessionId.length>200)throw Error('PLUGIN_SESSION_INVALID');
  let found=ctx.get('sessions')?.get(body.sessionId);
  if(!found){
   const persistence=ctx.get('sessionPersistence');
   try{found=await persistence?.stat(body.sessionId);}catch{found=undefined;}
  }
  if(!found)throw Error('PLUGIN_SESSION_NOT_FOUND');
 }
 // Skills must derive cwd from the authoritative stored session, or the host's
 // fixed workspace. A browser cannot enumerate another server directory.
 delete body.cwd;
 if(route==='skills'&&!body.sessionId&&config.workspaceRoot)body.cwd=config.workspaceRoot;
 return new Request(request.url,{method:'POST',headers:request.headers,body:JSON.stringify(body)});
}

export function contextFacade(ctx, config) {
 const wrapConnection=connection=>connection&&new Proxy(connection,{get(target,key){
  if(key!=='fetch')return Reflect.get(target,key,target);
  const face=target.fetch;
  return new Proxy(face,{get(fetch,key){if(key!=='register')return Reflect.get(fetch,key,fetch);
   return route=>fetch.register({...route,fetch:async request=>{
    try{
     const validated=await validateContextRequest(request,ctx,config);
     const body=await validated.clone().json();
     if(config.surface==='web'&&typeof body.sessionId==='string'){
      // The fixed server module carries the authenticated session into the
      // attachment broker. Browsers cannot choose the module or authority.
      const withScope=config.attachmentScope??(await import('/opt/personal-cloud/native-attachment-plugin.mjs')).withAttachmentScope;
      return await withScope({sessionId:body.sessionId,requestId:randomUUID()},()=>route.fetch(validated));
     }
     return await route.fetch(validated);
    }
    catch(error){return Response.json({ok:false,error:{code:'maimi/plugin-request-denied',message:String(error.message)}},{status:403,headers:{'cache-control':'no-store'}});}
   }});
  }});
 }});
 const wrap=target=>new Proxy(target,{get(target,key){
  if(key==='inject')return(deps,callback,...rest)=>target.inject(deps,child=>callback(wrap(child)),...rest);
  if(key==='get')return(name,...rest)=>name==='connection'?wrapConnection(target.get(name,...rest)):target.get(name,...rest);
  if(key==='connection')return wrapConnection(target.connection);
  const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;
 }});
 return wrap(ctx);
}

const SIDEBAR_UNAUTHORIZED=/^(?:sidechat\.(?:start|prompt|model|cancel|dequeue|dispose)|teams\.task(?:Create|Update)|workflows\.(?!list$)|subagents\.(?:stop|kill)|git\.(?:suggest|generate))/;
export function sidebarRouteAllowed(url, config={}) {
 const pathname=new URL(url,'http://localhost').pathname;
 if(!pathname.startsWith('/sidebar/'))return false;
 const method=pathname.slice('/sidebar/api/'.length);
 if(pathname.startsWith('/sidebar/api/')&&SIDEBAR_UNAUTHORIZED.test(method)&&config.authorizedExtraGeneration!==true)return false;
 // Unsafe same-origin HTML previews may not grant a document DSH credentials.
 if(pathname.startsWith('/sidebar/html')&&new URL(url,'http://localhost').searchParams.has('unsafe'))return false;
 return true;
}
export function sidebarFacade(ctx,config) {
 const webServer=new Proxy(ctx.webServer,{get(target,key){
  if(key!=='register'){const v=Reflect.get(target,key,target);return typeof v==='function'?v.bind(target):v;}
  return route=>target.register({...route,handler:async(req,res)=>{
   if(!sidebarRouteAllowed(req.url??'/',config)){res.writeHead(403,{'content-type':'application/json'});res.end(JSON.stringify({ok:false,error:{code:'maimi/task-authority-required',message:'此功能需要公司任务授权。'}}));return;}
   return route.handler(req,res);
  }});
 }});
 return new Proxy(ctx,{get(target,key){if(key==='webServer')return webServer;const v=Reflect.get(target,key,target);return typeof v==='function'?v.bind(target):v;}});
}
