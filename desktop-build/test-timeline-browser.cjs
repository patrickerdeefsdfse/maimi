'use strict';
const {app,BrowserWindow,session,nativeTheme}=require('electron');
nativeTheme.themeSource='light';
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),net=require('node:net');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {randomUUID}=require('node:crypto');
const assert=require('node:assert/strict');
const here=__dirname,baseline="D:\\麦格米特\\linux\\output\\experience-skills-20260924\\ui",runtime="C:\\Users\\LI YUHAO\\.codex\\tmp\\dsh020-r41-desktop",area=path.join(runtime,'ui-browser-fixtures');
fs.mkdirSync(area,{recursive:true});
const fixture=fs.mkdtempSync(path.join(area,'run-'));
const rawLog=path.join(fixture,'native.raw.log');
for(const stream of [process.stdout,process.stderr])stream?.on('error',error=>{
 if(error.code==='EPIPE'){fs.writeFileSync(path.join(fixture,'diagnostic-pipe-closed.json'),JSON.stringify({code:'EPIPE',test_result_preserved:true}));return;}
 void fatalFixture(error);
});
const bounded=work=>Promise.race([work,new Promise((_,reject)=>setTimeout(()=>reject(Error('FIXTURE_DIAGNOSTIC_TIMEOUT')),3000))]);

const syntheticUsername=String.raw`  LAB\operator  `,syntheticPassword=String.raw`  synthetic-P@ss\word&雪  `;
const fixtureClient=process.argv.includes('--desktop-fixture')?'desktop':'web';
const fixtureMode=process.argv.includes('--local-fixture')?'local':'cloud';
fs.writeFileSync(path.join(fixture,'owned-process.json'),JSON.stringify({pid:process.pid,entry:__filename,fixture,mode:fixtureMode})+'\n');
for(const name of ['profile','home','state','workspace','tmp'])fs.mkdirSync(path.join(fixture,name));
app.setPath('userData',path.join(fixture,'profile'));
for(const option of ['disable-background-networking','disable-component-update','disable-sync'])app.commandLine.appendSwitch(option);
app.on('window-all-closed',()=>{});
let child,server,window,output='',origin,upstream;
let asynchronousFailure=null,phase='startup';
function failedCallback(error){asynchronousFailure ||= error;fs.writeFileSync(path.join(fixture,'callback-failure.json'),JSON.stringify({phase,message:String(error.stack||error)})+'\n');}
function guarded(emitter){const on=emitter.on;emitter.on=function(event,listener){return on.call(this,event,function(...args){try{return listener.apply(this,args);}catch(error){failedCallback(error);}});};return emitter;}
async function fatalFixture(error){failedCallback(error);try{for(const socket of fixtureSockets)socket.destroy();server?.closeAllConnections();server?.close();await stopFixtureChild();window?.destroy();}finally{app.exit(1);}}
process.on('uncaughtException',error=>{void fatalFixture(error);});
process.on('unhandledRejection',error=>{void fatalFixture(error);});

const fixtureSockets=new Set();
function trackSocket(socket){fixtureSockets.add(socket);socket.once('close',()=>fixtureSockets.delete(socket));return socket;}
function cleanupStep(step){fs.writeFileSync(path.join(fixture,'cleanup-state.json'),JSON.stringify({step,pid:process.pid,childPid:child?.pid,childExitCode:child?.exitCode,childSignalCode:child?.signalCode,at:new Date().toISOString()})+'\n');}
async function stopFixtureChild(){
 if(!child||child.exitCode!==null||child.signalCode!==null)return;
 if(process.platform==='win32'){
  // Windows sends these signals through TerminateProcess. Do not wait for
  // libuv's child exit callback inside Electron shutdown: it can remain
  // pending even after the owned OS process is gone. The outer runner verifies
  // that no process referencing this exact fixture remains after app exit.
  const killed=child.kill('SIGKILL');
  if(!killed&&child.exitCode===null&&child.signalCode===null)throw Error('FIXTURE_NATIVE_TERMINATE_FAILED');
  child.stdout?.destroy();child.stderr?.destroy();child.unref();
  cleanupStep('WINDOWS_NATIVE_TERMINATE_REQUESTED');
  return;
 }
 // A signalled child keeps exitCode === null after it has already emitted
 // exit. Never subscribe to an event that can no longer happen indefinitely.
 let stopped;
 const exited=new Promise(resolve=>{stopped=resolve;child.once('exit',resolve);});
 try{
  cleanupStep('NATIVE_SIGTERM');
  child.kill('SIGTERM');
  cleanupStep('NATIVE_SIGTERM_SENT');
  await Promise.race([exited,delay(4000)]);
  if(child.exitCode===null&&child.signalCode===null){
   child.kill('SIGKILL');
   await Promise.race([exited,delay(2000)]);
  }
  if(child.exitCode===null&&child.signalCode===null)throw Error('FIXTURE_NATIVE_CLEANUP_TIMEOUT');
 }finally{child.removeListener('exit',stopped);}
}
const promptBodies=[],modelCalls=[],selectionRequests=[];const historyBySession=new Map();const zipBytes=fs.readFileSync(path.join(here,'synthetic-result.zip'));const syntheticCard=(connector,n)=>{const card=JSON.parse(JSON.stringify(projectFixtures.find(c=>c.connector===connector)));if(connector==='meeting'){card.title='会议文档';card.status='completed';card.actions=[];card.summary='已生成 2 份文档。';card.artifacts=[{attachment_id:ref('zip'+n),name:'会议文件-'+n+'.zip',media_type:'application/zip'},{attachment_id:ref('doc1'+n),name:'会议通知-'+n+'.docx',media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},{attachment_id:ref('doc2'+n),name:'会议决议-'+n+'.docx',media_type:'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}];card.sections=[{type:'notice',text:'INTERNAL_PROCESS_MUST_NOT_BE_VISIBLE'},{type:'table',title:'PROCEDURE',columns:[],rows:[]}];}return card;};
const errors=[],warnings=[],requests=[],openedAuthorizations=[],projectControls=[],downloads=[];let handoffCalls=0;
const fixtureFiles="D:\\麦格米特\\linux\\output\\meeting-skill-fix-20260924\\projects\\ui-fixture";
const mcpCalls=[];let mcpConnections=[];const syntheticMcpKey='SYNTHETIC_MCP_KEY_NEVER_PERSIST';
const oldCards=JSON.parse(fs.readFileSync(path.join(baseline,'../projects/result-fixtures.json'))).items;
const projectFixtures=[...oldCards.filter(card=>!['employee-cards','heatcost'].includes(card.connector)),JSON.parse(fs.readFileSync(path.join(fixtureFiles,'card-result.json'))),JSON.parse(fs.readFileSync(path.join(fixtureFiles,'cost-result.json')))];
for(const card of projectFixtures)if(card.connector==='infrastructure-monitor')for(const action of card.actions||[])if(action.form==='monitor-login'){action.fields=[{name:'location',type:'select',label:'地区',options:[{value:'synthetic',label:'合成地区',target:'192.0.2.10'}]},{name:'target',label:'IP',type:'text'},{name:'username',label:'账号'},{name:'password',label:'密码'}];}
const inlineRequests=[];const ref=value=>'pjf_'+Buffer.from(value).toString('base64url').padEnd(32,'A').slice(0,32);
for(const card of projectFixtures)for(const action of card.actions||[])for(const field of action.fields||[])if(field.name==='photo_attachment_id')for(const option of field.options||[])if(option.value)option.value=ref(option.value);

for(const card of projectFixtures)for(const artifact of card.artifacts||[])artifact.attachment_id='pjf_'+Buffer.from(artifact.attachment_id).toString('base64url').padEnd(32,'A').slice(0,32);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function reserve(){const server=net.createServer();server.listen(0,'127.0.0.1');await once(server,'listening');const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port;}
app.whenReady().then(async()=>{
 let code=1;
 try{
  const nativePort=await reserve();upstream='http://127.0.0.1:'+nativePort;
  const profileDir=path.join(fixture,'state/profiles/web');fs.mkdirSync(profileDir,{recursive:true});fs.writeFileSync(path.join(profileDir,'package.json'),JSON.stringify({name:'synthetic-profile',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}}));await (await import(require('node:url').pathToFileURL(path.join(here,'app/community-profile.mjs')))).prepareCommunityProfile({home:path.join(fixture,'state'),runtimeRoot:path.join(runtime,'runtime-win32-x64'),yaml:require(path.join(runtime,'runtime-win32-x64/node_modules/yaml'))});
  const fixturePatch=path.join(fixture,'profile.patch.json');fs.writeFileSync(fixturePatch,JSON.stringify((await import(require('node:url').pathToFileURL(path.join(here,'app/enterprise-profile.mjs')))).enterpriseProfile({cloud:false})));
  const logFd=fs.openSync(rawLog,'w');
  try{child=spawn('C:/Program Files/nodejs/node.exe',['--expose-internals',path.join(runtime,'runtime-win32-x64/node_modules/@deepseek-ai/dsh/lib/bin.js'),'--profile','web','--patch',fixturePatch,'--host','127.0.0.1','--port',String(nativePort),'--no-open'],{windowsHide:true,cwd:path.join(fixture,'workspace'),stdio:['ignore',logFd,logFd],env:{PATH:process.env.PATH,SystemRoot:process.env.SystemRoot,HOME:path.join(fixture,'home'),USERPROFILE:path.join(fixture,'home'),DSH_HOME:path.join(fixture,'state'),TEMP:path.join(fixture,'tmp'),TMP:path.join(fixture,'tmp'),NARB_DISABLE_NATIVE_CACHE:'1',DSH_TELEMETRY_DISABLED:'1',DSH_TELEMETRY_MODE:'DISABLED',MEGMEET_MODEL_TOKEN:'synthetic-only'}});}finally{fs.closeSync(logFd);}
  let token;
  for(let i=0;i<1200;i++){output=fs.readFileSync(rawLog,'utf8').slice(-131072);token=output.match(/http:\/\/127\.0\.0\.1:\d+\/\?token=([A-Za-z0-9_-]+)/)?.[1];if(token)break;if(child.exitCode!==null)throw Error('NATIVE_EXITED');await delay(250);}
  assert.ok(token,'NATIVE_BOOTSTRAP_TIMEOUT');
  server=http.createServer((req,res)=>{guarded(req);guarded(res);
   const url=new URL(req.url,'http://127.0.0.1');requests.push({path:url.pathname});
   if(url.pathname.startsWith('/plugins/@deepseek-ai/')){const match=/^\/plugins\/(@deepseek-ai\/[^/]+)\/client.js$/.exec(url.pathname),file=match?path.join(here,'overlays',match[1].split('/')[1],'lib/client.js'):'';if(file&&fs.existsSync(file)){res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(file));return;}}
   if(url.pathname==='/api/session/prompt'){let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{const body=JSON.parse(raw);promptBodies.push(body);if(server.rejectPrompt){res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({type:'server-response',rpcId:body.rpcId,result:{ok:false,error:{code:'session/agent-busy',message:'',details:{}}}}));}});if(server.rejectPrompt)return;}
   if(url.pathname==='/synthetic-model/chat/completions'){let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{const value=JSON.parse(raw);modelCalls.push({model:value.model,tools:!!value.tools?.length,selected:[...server.selected]});if(!value.stream){res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({id:'synthetic-title',choices:[{message:{role:'assistant',content:'Synthetic native send'},finish_reason:'stop'}]}));return;}const sid=promptBodies.at(-1).payload.args.request.sessionId;const connector=server.selected.find(id=>['meeting','employee-cards','heatcost','infrastructure-monitor'].includes(id));let reply='ORDINARY_REPLY_RETAINED';if(connector){const job_id=randomUUID(),rows=historyBySession.get(sid)||[];const row={job_id,connector,created_at:new Date().toISOString(),result:{status:'completed',project_result:syntheticCard(connector,rows.length+1)}};historyBySession.set(sid,[...rows,row]);reply=connector+'已完成。\n\n<!-- maimi-project-v1:'+job_id+' -->';}res.writeHead(200,{'Content-Type':'text/event-stream'});for(const [delta,finish_reason] of [[{role:'assistant',content:reply},null],[{},'stop']])res.write('data: '+JSON.stringify({id:'synthetic-send',object:'chat.completion.chunk',model:value.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');res.end('data: [DONE]\n\n');});return;}

   if(url.pathname==='/maimi/web/session.js') {res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(path.join(baseline,'session-script.js')));return;}
   if(url.pathname==='/plugins/@deepseek-ai/dsh-client-ui-brand-official/client.js') {res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(path.join(here,'overlays/dsh-client-ui-brand-official/lib/client.js')));return;}
   if(url.pathname.startsWith('/__megmeet/') || url.pathname==='/v1/personal-cloud/web/activity'){
    requests.push({path:url.pathname,method:req.method});
    const tools=[{plugin_key:'web-search',version:'1',name:'联网搜索',description:'搜索公开信息，读取网页并提供来源。',status:'ready'},
      {plugin_key:'dingtalk',version:'1',name:'钉钉',description:'在对话中处理消息、日程和待办。',status:mcpConnections.length?'ready':server.dingTalkStatus||'unavailable',status_message:mcpConnections.length?'个人 MCP 已连接。':'企业钉钉应用尚未配置，请联系管理员。',setup:{state:'connected',checks:[]},enterprise_oauth:{status:'authorization_required',status_message:'企业应用仍需连接本人钉钉账户。',authorization_url:'http://127.0.0.1/v1/connectors/dingtalk/connect',setup:{state:'authorization_required',checks:[{key:'current_account',label:'企业账户授权',ready:false}]}},capabilities:[{key:'message',label:'发送消息',available:false,requires_confirmation:true}]},
      {plugin_key:'meeting',version:'1',name:'会议文档',description:'根据议案生成会议文档。',status:'ready'},{plugin_key:'employee-cards',name:'工卡制作',status:'ready'},{plugin_key:'infrastructure-monitor',name:'集群监控',status:'ready'},{plugin_key:'heatcost',name:'图纸成本分析',status:'ready'}];
    if(url.pathname.startsWith('/__megmeet/enterprise/connectors/dingtalk/cli/')){
      const leaf=url.pathname.split('/').pop();req.resume();if(leaf==='login')server.cliState='authorizing';if(leaf==='disconnect')server.cliState='authorization_required';
      const value=leaf==='disconnect'?{status:'disconnected'}:{provider:'dingtalk_workspace_cli',version:'1.0.62',status:server.cliState||'authorization_required',status_message:'合成授权测试',authorization_url:'https://login.dingtalk.com/oauth2/device?synthetic=true',expires_at:Date.now()+60000,capabilities:[{product:'doc',label:'文档'}]};
      res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(value));return;
    }
    if(url.pathname.startsWith('/__megmeet/enterprise/connectors/dingtalk/mcp/')){
      const leaf=url.pathname.split('/').pop(),respond=(code,value)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(value));};
      if(req.method==='GET'&&leaf==='connections'){respond(200,{connections:mcpConnections,maximum_connections:16});return;}
      let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{const body=JSON.parse(raw);mcpCalls.push({leaf,has_url:typeof body.mcp_url==='string',valid_key:typeof body.mcp_url==='string'&&body.mcp_url.endsWith(syntheticMcpKey),connection_id:body.connection_id||null,name:body.name||null});
        if(server.mcpFailure){respond(503,{detail:'arbitrary upstream '+syntheticMcpKey});raw='';body.mcp_url='';return;}
        if(leaf==='connections'){assert.match(body.mcp_url,/^https:\/\/mcp-gw\.dingtalk\.com\/server\/synthetic\?key=/);const id=(mcpConnections.length+1).toString(16).padStart(32,'0');mcpConnections.push({id,name:body.name,status:'ready',tool_count:3,read_tool_count:2,write_tool_count:1});}
        if(leaf==='refresh')assert.ok(mcpConnections.some(c=>c.id===body.connection_id));
        if(leaf==='disconnect')mcpConnections=mcpConnections.filter(c=>c.id!==body.connection_id);
        raw='';body.mcp_url='';respond(200,{status:'ready'});
      });return;
    }
    if(url.pathname==='/__megmeet/enterprise/connectors/dingtalk/desktop-handoff'){handoffCalls++;res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({handoff:true,expires_in:60,authorization_url:'https://synthetic-company.invalid/v1/connectors/dingtalk/desktop-handoff#'+'A'.repeat(43)}));return;}
    if(url.pathname==='/__megmeet/tools/confirm'){let raw='';req.on('data',c=>raw+=c);req.on('end',()=>{const body=JSON.parse(raw);server.confirmCalls=(server.confirmCalls||0)+1;assert.equal(body.session_id,selectionRequests.at(-1).session_id);if(server.confirmMode==='network'){req.socket.destroy();return;}server.toolPending=[];res.writeHead(server.confirmMode==='unknown'?502:200,{'Content-Type':'application/json'});res.end(JSON.stringify(server.confirmMode==='unknown'?{detail:'dingtalk_mcp_write_result_unknown'}:{status:'completed',result:{isError:server.confirmMode==='isError',content:[{type:'text',text:'SYNTHETIC_PROVIDER_RESULT'}]}}));});return;}
    if(['/__megmeet/tools/invoke','/__megmeet/tools/cancel-job','/__megmeet/tools/form-ticket','/__megmeet/tools/secure-form'].includes(url.pathname)){
     let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{const body=JSON.parse(raw);if(url.pathname.endsWith('secure-form')){assert.equal(body.fields.password,syntheticPassword);assert.equal(body.fields.username,syntheticUsername);}projectControls.push({path:url.pathname,session_id:body.session_id,connector:body.connector,action:body.action,arguments:['edit_employee','revise'].includes(body.action)?body.arguments:undefined,has_password:typeof body.fields?.password==='string'&&body.fields.password.length>0,request_id:body.request_id});
      let result={status:'running',job_id:randomUUID()};if(url.pathname.endsWith('form-ticket'))result={ticket:'T'.repeat(43),form:'monitor-login',expires_in:30};else if(url.pathname.endsWith('cancel-job')){server.projectJob=false;result={status:'cancelled',job_id:'synthetic-job'};}else {
        const rows=historyBySession.get(body.session_id)||[],parent=rows.find(row=>row.job_id===body.parent_job_id);assert.ok(parent,'FOLLOWUP_PARENT_REQUIRED');
        const row={job_id:result.job_id,parent_job_id:parent.job_id,connector:parent.connector,created_at:new Date().toISOString(),result:{status:'completed',project_result:syntheticCard(parent.connector,rows.length+1)}};
        setTimeout(()=>historyBySession.set(body.session_id,[...(historyBySession.get(body.session_id)||[]),row]),350);server.projectJob=true;
      }
      res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(result));raw='';if(body.fields)body.fields.password='';});return;
    }
    if(url.pathname==='/__megmeet/projects/artifact'){assert.match(url.searchParams.get('attachment_id'),/^pjf_[A-Za-z0-9_-]{32}$/);assert.ok(url.searchParams.get('session_id'));if(url.searchParams.get('disposition')==='inline'){inlineRequests.push({session_id:url.searchParams.get('session_id'),attachment_id:url.searchParams.get('attachment_id')});res.writeHead(200,{'Content-Type':'image/png','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(fs.readFileSync(path.join(fixtureFiles,'card-preview-1.png')));return;}res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="synthetic-result.bin"'});res.end(zipBytes);return;}
    const value=url.pathname==='/__megmeet/desktop/workspace'?{mode:fixtureMode,client:fixtureClient}:
     url.pathname==='/__megmeet/desktop/models/refresh'?{status:'ready'}:
     url.pathname==='/__megmeet/tools/catalog'?(assert.ok(url.searchParams.get('session_id')),{items:tools}):
     url.pathname==='/__megmeet/tools/state'?{selected:server.selected,pending:[],results:[],jobs:[],history:(historyBySession.get(url.searchParams.get('session_id'))||[]).filter(row=>url.searchParams.has('history_job_ids')?JSON.parse(url.searchParams.get('history_job_ids')).includes(row.job_id):url.searchParams.has('history_children_of')?row.parent_job_id===url.searchParams.get('history_children_of'):false),history_next_cursor:null}:
     url.pathname==='/v1/personal-cloud/web/activity'?{status:'ready',busy:false}:
     url.pathname==='/__megmeet/enterprise/plugins'||url.pathname==='/__megmeet/enterprise/plugins/results'?{items:[]}:null;
    if(url.pathname==='/__megmeet/tools/selection'){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{selectionRequests.push(JSON.parse(body));server.selected=JSON.parse(body).plugins;res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({selected:server.selected}));});return;}
    res.writeHead(value?200:404,{'Content-Type':'application/json'});res.end(JSON.stringify(value||{error:'SYNTHETIC_ENDPOINT_NOT_IMPLEMENTED'}));return;
   }
   const forward=http.request(upstream+req.url,{method:req.method,headers:{...req.headers,'accept-encoding':'identity'}},response=>{guarded(response);
    if(response.headers['content-type']?.includes('text/html')||url.pathname==='/plugins/'){
     const chunks=[];response.on('data',b=>chunks.push(b));response.on('end',()=>{
      let body=Buffer.concat(chunks).toString().replaceAll('\r\n','\n');
      if(url.pathname==='/plugins/'){
       fs.writeFileSync(path.join(fixture,'plugins-response.txt'),body);
       for(const name of ['dsh-client-ui-brand-official','dsh-client-ui-conversation','dsh-client-ui-workspace','dsh-client-ui-settings-models','dsh-client-ui-chat']){
        const file=path.join(runtime,'runtime-win32-x64/node_modules/@deepseek-ai',name,'lib/client.js'),before=fs.readFileSync(file,'utf8').replaceAll('\r\n','\n').split('//# sourceMappingURL=')[0].trim(),after=fs.readFileSync(path.join(here,'overlays',name,'lib/client.js'),'utf8').replaceAll('\r\n','\n').trim();
        if(body.includes('id: "@deepseek-ai/'+name+'"')){if(!body.includes(before))errors.push('BUNDLE_NOT_REPLACED:'+name);else body=body.replace(before,()=>after);}
       }
      }else body=body.replace('</head>','<script src="/maimi/web/session.js"></script></head>');
      const headers={...response.headers};delete headers['content-length'];res.writeHead(response.statusCode,headers);res.end(body);
     });
    }else{res.writeHead(response.statusCode,response.headers);response.pipe(res);}});
   forward.on('error',()=>{if(!res.headersSent){res.writeHead(502);res.end();}else res.destroy();});req.pipe(forward);
  });
  server.selected=[];
  server.on('connection',trackSocket);
  server.on('upgrade',(req,socket,head)=>{
   trackSocket(socket);
   const remote=trackSocket(net.connect(nativePort,'127.0.0.1',()=>{remote.write(req.method+' '+req.url+' HTTP/'+req.httpVersion+'\r\n'+Object.entries(req.headers).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');if(head.length)remote.write(head);socket.pipe(remote);remote.pipe(socket);}));
   remote.on('error',()=>socket.destroy());socket.on('error',()=>remote.destroy());socket.on('close',()=>remote.destroy());
  });
  server.listen(0,'127.0.0.1');await once(server,'listening');origin='http://127.0.0.1:'+server.address().port;
  const browser=session.fromPartition('ui-test-'+Date.now());
  browser.on('will-download',(_event,item)=>{item.setSavePath(path.join(fixture,'synthetic-result.bin'));item.once('done',(_event,state)=>downloads.push({state,url:new URL(item.getURL()).pathname,bytes:item.getReceivedBytes()}));});
  browser.webRequest.onBeforeRequest((details,callback)=>{const url=new URL(details.url);const localSocket=['ws:','wss:'].includes(url.protocol)&&url.host===new URL(origin).host;callback({cancel:!['data:','blob:','devtools:'].includes(url.protocol)&&url.origin!==origin&&!localSocket});});
  window=new BrowserWindow({show:false,width:1440,height:1000,webPreferences:{session:browser,nodeIntegration:false,contextIsolation:true,sandbox:true,webSecurity:true,backgroundThrottling:false,offscreen:true}});
  window.webContents.setWindowOpenHandler(({url})=>{const u=new URL(url);openedAuthorizations.push({protocol:u.protocol,pathname:u.pathname,fragmentLength:u.hash.length-1});return {action:'deny'};});
  window.webContents.on('console-message',(_event,level,message)=>{if(level>=3){const line=String(message).slice(0,800);if(line==='ResizeObserver loop completed with undelivered notifications.'||(phase==='reload'&&/^Sidebar Session opening failed: Error: Session reference \"session-[a-f0-9-]+\" is released$/.test(line)))warnings.push(line);else{errors.push(line);fs.writeFileSync(path.join(fixture,'console-errors.json'),JSON.stringify({phase,errors}));}}});
  window.webContents.on('render-process-gone',(_event,details)=>errors.push('RENDER_GONE:'+details.reason));
  await window.loadURL(origin+'/?token='+token);
  const cookie=(await browser.cookies.get({url:origin})).map(item=>item.name+'='+item.value).join('; ');
  const rpc=async(method,args)=>{const response=await fetch(origin+'/api/'+method,{method:'POST',headers:{Cookie:cookie,Origin:origin,'Content-Type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:randomUUID(),method,payload:{args}}),signal:AbortSignal.timeout(15000)});const result=await response.json();assert.equal(result.result?.ok,true,JSON.stringify(result));return result.result.value;};
  await rpc('workspace/create',{request:{path:path.join(fixture,'workspace')}});
  const nativeSessionId=(await rpc('session/create',{request:{cwd:path.join(fixture,'workspace')}})).sessionId;
  const {providerPatch}=await import(require('node:url').pathToFileURL(path.join(here,'app/catalog.mjs')));
  const modelSection=providerPatch({data:[{id:'deepseek-v4.1-flash',name:'DeepSeek V4.1 Flash',inputModalities:['text']}],default_model:'deepseek-v4.1-flash'},origin+'/synthetic-model');
  await rpc('settings/replace',{ns:'llm-pi-ai',section:modelSection});
  await rpc('settings/update',{ns:'agent-default-model',patch:{provider:'deepseek-official',model:'deepseek-v4.1-flash'}});

  let state;
  for(let i=0;i<160;i++){
   state=await window.webContents.executeJavaScript('({text:document.body.innerText, logo:!!document.querySelector("[data-megmeet-sidebar-image]"), hero:!!document.querySelector("[data-megmeet-hero-image]"), cloud:!!document.querySelector("[data-megmeet-cloud-open]"), plugin:!!document.querySelector("[data-megmeet-plugin-picker]")})');
   if(state.logo&&state.cloud&&state.hero&&!state.text.includes('重新连接中'))break;await delay(250);
  }
  if(fixtureMode==='cloud')await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-cloud-open]").click()');
  for(let i=0;i<120;i++){
   state=await window.webContents.executeJavaScript('({text:document.body.innerText, logo:!!document.querySelector("[data-megmeet-sidebar-image]"), hero:!!document.querySelector("[data-megmeet-hero-image]"), cloud:!!document.querySelector("[data-megmeet-cloud-open]"), plugin:!!document.querySelector("[data-megmeet-plugin-picker]")})');
   if(state.plugin||fixtureMode==='local')break;await delay(250);
  }
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("button")).find(el=>el.textContent.trim()==="稍后配置")?.click()');
  await delay(750);
  const waitFor=async(fn,label)=>{phase=label;for(let i=0;i<100;i++){if(asynchronousFailure)throw asynchronousFailure;const value=await fn();if(value)return value;await delay(150);}throw Error(label);};
  const openSkills=async()=>{await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-plugin-picker]:not(:disabled)")'),'SKILL_BUTTON_READY');await window.webContents.executeJavaScript('if(!document.querySelector("[data-megmeet-plugin-key=meeting]"))document.querySelector("[data-megmeet-plugin-picker]").click()');await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-plugin-key=meeting]")'),'CATALOG_NOT_READY');await delay(300);};
  const send=async text=>{await window.webContents.executeJavaScript('document.querySelector("[contenteditable=true]").focus()');await window.webContents.insertText(text);await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("button[aria-label=发送消息]:not(:disabled)")'),'SEND_READY');await window.webContents.executeJavaScript('document.querySelector("button[aria-label=发送消息]").click()');await waitFor(()=>window.webContents.executeJavaScript('!document.querySelector("button[aria-label=停止]")'),'SEND_FINISHED');};
  assert.equal(await window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-desktop-updates]")'),false);
  await openSkills();await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-search-policy=official-first]")'),'CLOUD_SEARCH_POLICY');await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-picker]").click()');
  await send('ORDINARY_QUESTION_RETAINED');await waitFor(()=>window.webContents.executeJavaScript('document.body.innerText.includes("ORDINARY_REPLY_RETAINED")'),'ORDINARY_REPLY');
  await openSkills();await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-key=meeting]").click()');await waitFor(()=>Promise.resolve(server.selected.includes('meeting')),'SELECT_MEETING');
  const workbook=fs.readFileSync(path.join(here,'synthetic.xlsx')).toString('base64');
  await window.webContents.executeJavaScript(`(()=>{const input=document.querySelector('input[type=file]'),dt=new DataTransfer();dt.items.add(new File([Uint8Array.from(atob(${JSON.stringify(workbook)}),c=>c.charCodeAt(0))],'synthetic-meeting.xlsx',{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await waitFor(()=>window.webContents.executeJavaScript('document.body.innerText.includes("synthetic-meeting.xlsx")'),'UPLOAD_READY');
  await send('FIRST_MEETING_REQUEST');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-project-chat-node]").length===1'),'FIRST_NATIVE_PROJECT_NODE');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-download-zip]").length===1'),'FIRST_ZIP');
  await send('SECOND_MEETING_REQUEST');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-project-chat-node]").length===2'),'SECOND_NATIVE_PROJECT_NODE');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-download-zip]").length===2'),'SECOND_ZIP');
  const text=await window.webContents.executeJavaScript('document.body.innerText');assert.ok(text.includes('ORDINARY_QUESTION_RETAINED')&&text.includes('ORDINARY_REPLY_RETAINED'));assert.ok(text.includes('会议通知-1.docx')&&text.includes('会议通知-2.docx'));assert.ok(!text.includes('INTERNAL_PROCESS_MUST_NOT_BE_VISIBLE'));assert.ok(!text.includes('maimi-project-v1'));
  const geometry=await window.webContents.executeJavaScript(`(()=>{const cards=[...document.querySelectorAll('[data-megmeet-project-chat-node]')],composer=document.querySelector('[contenteditable=true]');return {nodes:cards.map(el=>({id:el.dataset.megmeetProjectChatNode,ancestors:[...function*(n){while(n){yield n.className;n=n.parentElement;}}(el)].slice(0,8),top:el.getBoundingClientRect().top})),insideComposer:cards.some(el=>el.contains(composer)||composer.parentElement.contains(el))};})()`);assert.equal(geometry.insideComposer,false);assert.ok(geometry.nodes[0].top<geometry.nodes[1].top);
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-download-zip]").click()');await waitFor(()=>Promise.resolve(downloads.length),'ZIP_DOWNLOADED');assert.equal(downloads[0].state,'completed');assert.equal(downloads[0].bytes,zipBytes.length);assert.deepEqual(fs.readFileSync(path.join(fixture,'synthetic-result.bin')),zipBytes);
  fs.writeFileSync(path.join(fixture,'two-project-results-native-chat.png'),(await window.webContents.capturePage()).toPNG());
  const active=promptBodies.at(-1).payload.args.request.sessionId;const previousUrl=window.webContents.getURL();
  phase='reload';await window.reload();await delay(2500);await waitFor(()=>window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[role=treeitem]")).some(el=>el.innerText.includes("ORDINARY_REPLY_RETAINED"))'),'HISTORY_SIDEBAR');
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[role=treeitem]")).find(el=>el.innerText.includes("ORDINARY_REPLY_RETAINED")).click()');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-download-zip]").length===2'),'HISTORY_REPLAY_AFTER_RELOAD');
  const replay=await window.webContents.executeJavaScript('document.body.innerText');assert.ok(replay.includes('ORDINARY_REPLY_RETAINED')&&replay.includes('会议通知-1.docx')&&replay.includes('会议通知-2.docx'));
  const links=await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("a[href]")).map(e=>({text:e.innerText,href:e.getAttribute("href")}))');
  fs.writeFileSync(path.join(fixture,'navigation-links.json'),JSON.stringify({active,previousUrl,links},null,2));
  const choose=async key=>{await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[data-megmeet-selected-skill] button")).forEach(button=>button.click())');await waitFor(()=>Promise.resolve(server.selected.length===0),'CLEAR_SELECTION');await openSkills();await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-plugin-key="${key}"]').click()`);await waitFor(()=>Promise.resolve(server.selected.includes(key)),'SELECT_NEXT_PROJECT');};
  for(const key of ['employee-cards','heatcost','infrastructure-monitor']){await choose(key);await send('SYNTHETIC_'+key);await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('[data-megmeet-project-result="${key}"]')`),'FOUR_NATIVE_CARDS_'+key);}
  const monitorRoot=historyBySession.get(active).find(row=>row.connector==='infrastructure-monitor').job_id;
  await window.webContents.executeJavaScript(`(()=>{const c=document.querySelector('[data-megmeet-project-result=infrastructure-monitor]');c.scrollIntoView({block:'center'});Array.from(c.querySelectorAll('button')).find(b=>b.textContent==='连接集群').click();})()`);
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("input[type=password]")'),'VMWARE_FORM_READY');
  await window.webContents.executeJavaScript(`(()=>{const f=document.querySelector('input[type=password]').form;f.elements.namedItem('username').value=${JSON.stringify(syntheticUsername)};f.elements.namedItem('password').value=${JSON.stringify(syntheticPassword)};f.requestSubmit();})()`);
  await waitFor(()=>Promise.resolve(projectControls.some(v=>v.path.endsWith('/secure-form')&&v.has_password)),'VMWARE_FORM_SUBMITTED_EXACT');
  await waitFor(()=>window.webContents.executeJavaScript(`document.querySelectorAll('[data-megmeet-project-job-history="${monitorRoot}"] [data-megmeet-project-result=infrastructure-monitor]').length===2`),'VMWARE_RESULT_APPENDED');
  assert.equal(await window.webContents.executeJavaScript('!!document.querySelector("input[type=password]")'),false);
  const costRoot=historyBySession.get(active).find(row=>row.connector==='heatcost').job_id;
  const costCard=projectFixtures.find(card=>card.connector==='heatcost');const editLabel=costCard.actions.find(action=>action.mode==='field').label;
  for(let n=1;n<=2;n++){
    await window.webContents.executeJavaScript(`(()=>{const cards=[...document.querySelectorAll('[data-megmeet-project-result=heatcost]')],card=cards.at(-1);card.scrollIntoView({block:'center'});Array.from(card.querySelectorAll('button')).find(button=>button.textContent===${JSON.stringify(editLabel)}).click();})()`);
    await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-project-form=cost-edit]")'),'REVIEW_FORM_OPEN');
    if(n===1){window.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});window.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'});await waitFor(()=>window.webContents.executeJavaScript('!document.querySelector("[data-megmeet-project-form=cost-edit]")'),'REVIEW_ESCAPE_CLOSE');await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('[data-megmeet-project-result=heatcost] button')).find(button=>button.textContent===${JSON.stringify(editLabel)}).click()`);}
    await window.webContents.executeJavaScript(`(()=>{const f=document.querySelector('[data-megmeet-project-form=cost-edit]');f.elements.namedItem('key').value='length';f.elements.namedItem('value').value='${n*123}';f.requestSubmit();})()`);
    await waitFor(()=>window.webContents.executeJavaScript(`document.querySelectorAll('[data-megmeet-project-job-history="${costRoot}"] [data-megmeet-project-result=heatcost]').length===${n+1}`),'FOLLOWUP_APPENDS_'+n);
  }
  assert.equal(await window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-project-chat-node]").length'),5);
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("button")).find(button=>button.textContent.trim().startsWith("新会话")).click()');await waitFor(()=>window.webContents.executeJavaScript('!document.querySelector("[data-megmeet-project-chat-node]")'),'NEW_SESSION_CLEAR');
  // Native new-session navigation releases the previous binding asynchronously.
  // Wait for a usable fresh composer before opening an older sidebar session.
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[contenteditable=true]")&&!document.body.innerText.includes("重新连接中")'),'NEW_SESSION_READY');await delay(3000);
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[role=treeitem]")).find(el=>el.innerText.includes("ORDINARY_REPLY_RETAINED")).click()');
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-project-chat-node]").length===5'),'SWITCH_BACK_HISTORY');
  await waitFor(()=>window.webContents.executeJavaScript(`document.querySelectorAll('[data-megmeet-project-job-history="${costRoot}"] [data-megmeet-project-result=heatcost]').length===3`),'FOLLOWUP_REPLAY');
  fs.writeFileSync(path.join(fixture,'four-skills-and-followup-history.png'),(await window.webContents.capturePage()).toPNG());
  if(asynchronousFailure)throw asynchronousFailure;await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Token 总览')?.click()`);await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('nav[aria-label="Token 总览"]')`),'TOKENMETER_CENTER_READY');fs.writeFileSync(path.join(fixture,'tokenmeter020-center.png'),(await window.webContents.capturePage()).toPNG());await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button[title]')).find(b=>b.title.startsWith('弹出'))?.click()`);await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('[role=dialog][data-widget]')`),'TOKENMETER_FLOAT_READY');await delay(500);fs.writeFileSync(path.join(fixture,'tokenmeter020-float.png'),(await window.webContents.capturePage()).toPNG());await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('nav[aria-label="Token 总览"] button')).find(b=>b.textContent.includes('设置'))?.click()`);await delay(500);fs.writeFileSync(path.join(fixture,'tokenmeter020-settings.png'),(await window.webContents.capturePage()).toPNG());
  assert.deepEqual(errors,[]);
  const result={schema:'r30-search-ui-native-browser-v1',status:'PASS',fixture,runtime:'0.2.0-rc.2',checks:{vmware_raw_credentials_preserved:true,vmware_human_result_appended:true,ordinary_chat_retained:true,two_skill_messages_append:true,native_conversation_event_nodes:true,no_composer_results:true,no_internal_process_or_marker_visible:true,meeting_zip_download_exact_bytes:true,full_reload_replays_native_history:true,all_four_projects_native:true,two_human_followups_append:true,review_escape_closes:true,session_switch_replays_history:true,followup_replay_keeps_original:true},warnings,geometry,boundary:'Actual rc2 Node host and Chromium native UI/upload/prompt/persistent history. Synthetic local project history/model/authorization fixture; company not accessed.',errors};
  fs.writeFileSync(path.join(here,'browser-result.json'),JSON.stringify(result,null,2));fs.writeFileSync(path.join(fixture,'result.json'),JSON.stringify(result,null,2));code=0;

 }catch(error){fs.writeFileSync(path.join(fixture,'failure.txt'),String(error.stack));fs.writeFileSync(path.join(fixture,'failure.json'),JSON.stringify({status:'FAIL',phase,reason:String(error.message).slice(0,500)}));try{if(window&&!window.isDestroyed()){fs.writeFileSync(path.join(fixture,'failed.png'),(await bounded(window.webContents.capturePage())).toPNG());fs.writeFileSync(path.join(fixture,'failed.json'),JSON.stringify({requests,errors,html:await bounded(window.webContents.executeJavaScript('document.body.innerHTML'))},null,2));}}catch{}}
 finally{
  try{
   cleanupStep('SOCKETS_DESTROY');
   for(const socket of fixtureSockets)socket.destroy();
   if(server){server.closeAllConnections();server.close();}
   cleanupStep('NATIVE_STOP');
   await stopFixtureChild();
   // Keep the hidden Electron window alive until Node's process callbacks
   // settle; destroying the final window first can suspend that integration.
   cleanupStep('WINDOW_DESTROY');
   if(window&&!window.isDestroyed())window.destroy();
   cleanupStep('LOG_WRITE');
   output=fs.existsSync(rawLog)?fs.readFileSync(rawLog,'utf8').slice(-131072):output;
   fs.writeFileSync(path.join(fixture,'native.log'),output.replace(/token=[A-Za-z0-9_-]+/g,'token=[synthetic-redacted]'));
   if(fs.existsSync(rawLog))fs.unlinkSync(rawLog);
  }catch(error){code=1;fs.writeFileSync(path.join(fixture,'cleanup-failure.json'),JSON.stringify({status:'FAIL',reason:'FIXTURE_CLEANUP_FAILED',error:String(error.message).slice(0,500)}));}
  finally{cleanupStep('APP_EXIT');app.exit(code);}
 }
});
