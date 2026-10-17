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
const fixtureClient=process.argv.includes('--desktop-fixture')?'desktop':'web';
const fixtureMode=process.argv.includes('--local-fixture')?'local':'cloud';
fs.writeFileSync(path.join(fixture,'owned-process.json'),JSON.stringify({pid:process.pid,entry:__filename,fixture,mode:fixtureMode})+'\n');
for(const name of ['profile','home','state','workspace','tmp'])fs.mkdirSync(path.join(fixture,name));
app.setPath('userData',path.join(fixture,'profile'));
for(const option of ['disable-background-networking','disable-component-update','disable-sync'])app.commandLine.appendSwitch(option);
app.on('window-all-closed',()=>{});
let child,server,window,output='',origin,upstream;
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
const promptBodies=[],modelCalls=[],selectionRequests=[];
const errors=[],warnings=[],requests=[],openedAuthorizations=[],projectControls=[],downloads=[];let handoffCalls=0;
const fixtureFiles="D:\\麦格米特\\linux\\output\\meeting-skill-fix-20260924\\projects\\ui-fixture";
const mcpCalls=[];let mcpConnections=[];const syntheticMcpKey='SYNTHETIC_MCP_KEY_NEVER_PERSIST';
const oldCards=JSON.parse(fs.readFileSync(path.join(baseline,'../projects/result-fixtures.json'))).items;
const projectFixtures=[...oldCards.filter(card=>!['employee-cards','heatcost'].includes(card.connector)),JSON.parse(fs.readFileSync(path.join(fixtureFiles,'card-result.json'))),JSON.parse(fs.readFileSync(path.join(fixtureFiles,'cost-result.json')))];
const inlineRequests=[];const ref=value=>'pjf_'+Buffer.from(value).toString('base64url').padEnd(32,'A').slice(0,32);
for(const card of projectFixtures)for(const action of card.actions||[])for(const field of action.fields||[])if(field.name==='photo_attachment_id')for(const option of field.options||[])if(option.value)option.value=ref(option.value);

for(const card of projectFixtures)for(const artifact of card.artifacts||[])artifact.attachment_id='pjf_'+Buffer.from(artifact.attachment_id).toString('base64url').padEnd(32,'A').slice(0,32);
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function reserve(){const server=net.createServer();server.listen(0,'127.0.0.1');await once(server,'listening');const port=server.address().port;await new Promise(resolve=>server.close(resolve));return port;}
app.whenReady().then(async()=>{
 let code=1;
 try{
  const nativePort=await reserve();upstream='http://127.0.0.1:'+nativePort;
  const fixturePatch=path.join(fixture,'profile.patch.json');fs.writeFileSync(fixturePatch,JSON.stringify((await import(require('node:url').pathToFileURL(path.join(here,'app/enterprise-profile.mjs')))).enterpriseProfile({cloud:false})));
  const logFd=fs.openSync(rawLog,'w');
  try{child=spawn('C:/Program Files/nodejs/node.exe',['--expose-internals',path.join(runtime,'runtime-win32-x64/node_modules/@deepseek-ai/dsh/lib/bin.js'),'--profile','web','--patch',fixturePatch,'--host','127.0.0.1','--port',String(nativePort),'--no-open'],{windowsHide:true,cwd:path.join(fixture,'workspace'),stdio:['ignore',logFd,logFd],env:{PATH:process.env.PATH,SystemRoot:process.env.SystemRoot,HOME:path.join(fixture,'home'),USERPROFILE:path.join(fixture,'home'),DSH_HOME:path.join(fixture,'state'),TEMP:path.join(fixture,'tmp'),TMP:path.join(fixture,'tmp'),NARB_DISABLE_NATIVE_CACHE:'1',DSH_TELEMETRY_DISABLED:'1',DSH_TELEMETRY_MODE:'DISABLED',MEGMEET_MODEL_TOKEN:'synthetic-only'}});}finally{fs.closeSync(logFd);}
  let token;
  for(let i=0;i<1200;i++){output=fs.readFileSync(rawLog,'utf8').slice(-131072);token=output.match(/http:\/\/127\.0\.0\.1:\d+\/\?token=([A-Za-z0-9_-]+)/)?.[1];if(token)break;if(child.exitCode!==null)throw Error('NATIVE_EXITED');await delay(250);}
  assert.ok(token,'NATIVE_BOOTSTRAP_TIMEOUT');
  server=http.createServer((req,res)=>{
   const url=new URL(req.url,'http://127.0.0.1');requests.push({path:url.pathname});
   if(url.pathname.startsWith('/plugins/@deepseek-ai/')){const match=/^\/plugins\/(@deepseek-ai\/[^/]+)\/client.js$/.exec(url.pathname),file=match?path.join(here,'overlays',match[1].split('/')[1],'lib/client.js'):'';if(file&&fs.existsSync(file)){res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(file));return;}}
   if(url.pathname==='/api/session/prompt'){let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{const body=JSON.parse(raw);promptBodies.push(body);if(server.rejectPrompt){res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({type:'server-response',rpcId:body.rpcId,result:{ok:false,error:{code:'session/agent-busy',message:'',details:{}}}}));}});if(server.rejectPrompt)return;}
   if(url.pathname==='/synthetic-model/chat/completions'){let raw='';req.on('data',b=>raw+=b);req.on('end',()=>{const value=JSON.parse(raw);modelCalls.push({model:value.model,tools:!!value.tools?.length,selected:[...server.selected]});if(!value.stream){res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({id:'synthetic-title',choices:[{message:{role:'assistant',content:'Synthetic native send'},finish_reason:'stop'}]}));return;}res.writeHead(200,{'Content-Type':'text/event-stream'});for(const [delta,finish_reason] of [[{role:'assistant',content:'SYNTHETIC_NATIVE_SEND_ACCEPTED'},null],[{},'stop']])res.write('data: '+JSON.stringify({id:'synthetic-send',object:'chat.completion.chunk',model:value.model,choices:[{index:0,delta,finish_reason}]})+'\n\n');res.end('data: [DONE]\n\n');});return;}

   if(url.pathname==='/maimi/web/session.js') {res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(path.join(baseline,'session-script.js')));return;}
   if(url.pathname==='/plugins/@deepseek-ai/dsh-client-ui-brand-official/client.js') {res.writeHead(200,{'Content-Type':'text/javascript'});res.end(fs.readFileSync(path.join(here,'overlays/dsh-client-ui-brand-official/lib/client.js')));return;}
   if(url.pathname.startsWith('/__megmeet/') || url.pathname==='/v1/personal-cloud/web/activity'){
    requests.push({path:url.pathname,method:req.method});
    const tools=[{plugin_key:'web',version:'1',name:'联网搜索',description:'搜索公开信息，读取网页并提供来源。',status:'ready'},
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
     let raw='';req.on('data',chunk=>raw+=chunk);req.on('end',()=>{const body=JSON.parse(raw);projectControls.push({path:url.pathname,session_id:body.session_id,connector:body.connector,action:body.action,arguments:['edit_employee','revise'].includes(body.action)?body.arguments:undefined,has_password:typeof body.fields?.password==='string'&&body.fields.password.length>0,request_id:body.request_id});
      let result={status:'running',job_id:'synthetic-job'};if(url.pathname.endsWith('form-ticket'))result={ticket:'T'.repeat(43),form:'monitor-login',expires_in:30};else if(url.pathname.endsWith('cancel-job')){server.projectJob=false;result={status:'cancelled',job_id:'synthetic-job'};}else server.projectJob=true;
      res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify(result));raw='';if(body.fields)body.fields.password='';});return;
    }
    if(url.pathname==='/__megmeet/projects/artifact'){assert.match(url.searchParams.get('attachment_id'),/^pjf_[A-Za-z0-9_-]{32}$/);assert.ok(url.searchParams.get('session_id'));if(url.searchParams.get('disposition')==='inline'){inlineRequests.push({session_id:url.searchParams.get('session_id'),attachment_id:url.searchParams.get('attachment_id')});res.writeHead(200,{'Content-Type':'image/png','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'});res.end(fs.readFileSync(path.join(fixtureFiles,'card-preview-1.png')));return;}res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="synthetic-result.bin"'});res.end('synthetic-only');return;}
    const value=url.pathname==='/__megmeet/desktop/workspace'?{mode:fixtureMode,client:fixtureClient}:
     url.pathname==='/__megmeet/desktop/models/refresh'?{status:'ready'}:
     url.pathname==='/__megmeet/tools/catalog'?(assert.ok(url.searchParams.get('session_id')),{items:tools}):
     url.pathname==='/__megmeet/tools/state'?{selected:server.selected,pending:server.toolPending||[],results:server.showResults?projectFixtures:[],jobs:server.failedJob?[server.failedJob]:server.projectJob?[{status:'running',job_id:'synthetic-job'}]:[]}:
     url.pathname==='/v1/personal-cloud/web/activity'?{status:'ready',busy:false}:
     url.pathname==='/__megmeet/enterprise/plugins'||url.pathname==='/__megmeet/enterprise/plugins/results'?{items:[]}:null;
    if(url.pathname==='/__megmeet/tools/selection'){let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{selectionRequests.push(JSON.parse(body));server.selected=JSON.parse(body).plugins;res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({selected:server.selected}));});return;}
    res.writeHead(value?200:404,{'Content-Type':'application/json'});res.end(JSON.stringify(value||{error:'SYNTHETIC_ENDPOINT_NOT_IMPLEMENTED'}));return;
   }
   const forward=http.request(upstream+req.url,{method:req.method,headers:{...req.headers,'accept-encoding':'identity'}},response=>{
    if(response.headers['content-type']?.includes('text/html')||url.pathname==='/plugins/'){
     const chunks=[];response.on('data',b=>chunks.push(b));response.on('end',()=>{
      let body=Buffer.concat(chunks).toString().replaceAll('\r\n','\n');
      if(url.pathname==='/plugins/'){
       fs.writeFileSync(path.join(fixture,'plugins-response.txt'),body);
       for(const name of ['dsh-client-ui-brand-official','dsh-client-ui-conversation','dsh-client-ui-workspace','dsh-client-ui-settings-models']){
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
  window.webContents.on('console-message',(_event,level,message)=>{if(level>=3){const line=String(message).slice(0,800);if(line==='ResizeObserver loop completed with undelivered notifications.')warnings.push(line);else errors.push(line);}});
  window.webContents.on('render-process-gone',(_event,details)=>errors.push('RENDER_GONE:'+details.reason));
  await window.loadURL(origin+'/?token='+token);
  const cookie=(await browser.cookies.get({url:origin})).map(item=>item.name+'='+item.value).join('; ');
  const rpc=async(method,args)=>{const response=await fetch(origin+'/api/'+method,{method:'POST',headers:{Cookie:cookie,Origin:origin,'Content-Type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:randomUUID(),method,payload:{args}}),signal:AbortSignal.timeout(15000)});const result=await response.json();assert.equal(result.result?.ok,true,JSON.stringify(result));return result.result.value;};
  await rpc('workspace/create',{request:{path:path.join(fixture,'workspace')}});
  const nativeSessionId=(await rpc('session/create',{request:{cwd:path.join(fixture,'workspace')}})).sessionId;
  const {providerPatch}=await import(require('node:url').pathToFileURL(path.join(here,'app/catalog.mjs')));
  const modelSection=providerPatch({data:[{id:'synthetic-model',name:'Synthetic',inputModalities:['text']}],default_model:'synthetic-model'},origin+'/synthetic-model');modelSection.providers['synthetic-fixture']=modelSection.providers['deepseek-official'];delete modelSection.providers['deepseek-official'];
  await rpc('settings/replace',{ns:'llm-pi-ai',section:modelSection});
  await rpc('settings/update',{ns:'agent-default-model',patch:{provider:'synthetic-fixture',model:'synthetic-model'}});

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
  const waitFor=async(fn,label)=>{for(let i=0;i<100;i++){const value=await fn();if(value)return value;await delay(150);}throw Error(label);};
  const openSkills=async()=>{await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-picker]").click()');await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-plugin-key=meeting]")'),'CATALOG_NOT_READY');await delay(300);};
  await openSkills();
  const directory=await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[data-megmeet-plugin-key]")).map(el=>({key:el.dataset.megmeetPluginKey,text:el.innerText,workflow:!!el.querySelector("[data-megmeet-workflow-badge]"),behavior:el.querySelector("[data-megmeet-skill-behavior]")?.dataset.megmeetSkillBehavior}))');
  assert.deepEqual(directory.filter(x=>x.workflow).map(x=>x.key).sort(),['employee-cards','heatcost','infrastructure-monitor','meeting']);
  assert.match(directory.find(x=>x.key==='infrastructure-monitor').text,/vmware/);
  assert.equal(directory.find(x=>x.key==='web').behavior,'model');assert.equal(directory.find(x=>x.key==='web').workflow,false);
  assert.equal(projectControls.length,0);assert.equal(promptBodies.length,0);
  fs.writeFileSync(path.join(fixture,'workflow-skills-light.png'),(await window.webContents.capturePage()).toPNG());
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-key=meeting]").click()');
  await waitFor(()=>Promise.resolve(server.selected.includes('meeting')),'SELECTION_NOT_STORED');
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-selected-skill=meeting]")'),'MEETING_CHIP_NOT_RENDERED');
  assert.equal(projectControls.length,0);assert.equal(promptBodies.length,0);
  const selectedHint=await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-selected-skill=meeting]").innerText');assert.match(selectedHint,/发送后执行/);
  await window.webContents.executeJavaScript('document.querySelector("[contenteditable=true]").focus()');await window.webContents.insertText('SYNTHETIC_MEETING_SUBMISSION');await delay(150);
  const draftBefore=await window.webContents.executeJavaScript('document.querySelector("[contenteditable=true]").innerText');assert.equal(draftBefore,'SYNTHETIC_MEETING_SUBMISSION');
  await openSkills();await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-key=web]").click()');await waitFor(()=>Promise.resolve(server.selected.includes('web')),'OPTIONAL_SELECTION_NOT_STORED');
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-selected-skill=web]")'),'OPTIONAL_CHIP_NOT_RENDERED');
  assert.equal(await window.webContents.executeJavaScript('document.querySelector("[contenteditable=true]").innerText'),draftBefore);
  assert.equal(await window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-selected-skill=web] [data-megmeet-workflow-selected]")'),false);
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-selected-skill=web] button").click()');await waitFor(()=>Promise.resolve(!server.selected.includes('web')),'OPTIONAL_REMOVAL_FAILED');
  const workbook=fs.readFileSync(path.join(here,'synthetic.xlsx')).toString('base64');
  await window.webContents.executeJavaScript(`(()=>{const input=document.querySelector('input[type=file]'),dt=new DataTransfer();dt.items.add(new File([Uint8Array.from(atob(${JSON.stringify(workbook)}),c=>c.charCodeAt(0))],'synthetic-meeting.xlsx',{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await waitFor(()=>window.webContents.executeJavaScript('document.body.innerText.includes("synthetic-meeting.xlsx") && !!document.querySelector("button[aria-label=发送消息]:not(:disabled)")'),'NATIVE_ATTACHMENT_READY');
  assert.equal(promptBodies.length,0);assert.equal(projectControls.length,0);
  fs.writeFileSync(path.join(fixture,'workflow-selected-attachment.png'),(await window.webContents.capturePage()).toPNG());
  await window.webContents.executeJavaScript('document.querySelector("button[aria-label=发送消息]").click()');
  await waitFor(()=>Promise.resolve(promptBodies.length===1),'NATIVE_PROMPT_NOT_SENT');
  await waitFor(()=>window.webContents.executeJavaScript('document.body.innerText.includes("SYNTHETIC_NATIVE_SEND_ACCEPTED")'),'NATIVE_REPLY_NOT_RENDERED');
  const admitted=promptBodies[0].payload.args.request;
  // The native hero may create a fresh session when entering the cloud view;
  // the selected session, rather than the earlier fixture seed, owns the send.
  assert.equal(admitted.sessionId,selectionRequests[0].session_id);assert.match(admitted.requestId,/^[a-f0-9-]{36}$/i);
  assert.deepEqual(admitted.content.filter(x=>x.type==='text').map(x=>x.text),[draftBefore]);
  assert.equal(admitted.content.filter(x=>x.type==='file').length,1);assert.ok(admitted.content.find(x=>x.type==='file').receiptId);
  assert.equal(projectControls.length,0);assert.ok(modelCalls.some(x=>x.tools&&x.selected.includes('meeting')));
  assert.equal(await window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-project-results]")'),false);
  server.showResults=true;
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-project-result=meeting]")'),'MEETING_CARDS_NOT_RENDERED');
  assert.ok(await window.webContents.executeJavaScript('document.querySelectorAll("[data-megmeet-project-result=meeting] button[title=下载文件]").length'));
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-project-result=meeting] button[title=下载文件]").click()');await waitFor(()=>Promise.resolve(downloads.length),'MEETING_DOWNLOAD_NOT_TRIGGERED');assert.equal(downloads[0].state,'completed');
  const cardFixture=projectFixtures.find(card=>card.connector==='employee-cards');
  await window.webContents.executeJavaScript(`(()=>{const card=document.querySelector('[data-megmeet-project-result=employee-cards]');card.scrollIntoView({block:'start'});Array.from(card.querySelectorAll('button')).find(el=>el.textContent==='修改信息或补照').click();})()`);
  assert.equal(await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-form=card-edit] select[name=employee_id]').value`),'');
  const setForm=async(selector,values)=>window.webContents.executeJavaScript(`(()=>{const f=document.querySelector(${JSON.stringify(selector)});for(const [k,v]of Object.entries(${JSON.stringify(values)})){const input=f.elements.namedItem(k);input.value=v;input.dispatchEvent(new Event('change',{bubbles:true}));}return true;})()`);
  const employee=cardFixture.actions.find(action=>action.form==='card-edit').fields.find(field=>field.name==='employee_id').options[0].value;
  await setForm('[data-megmeet-project-form=card-edit]',{employee_id:employee,name:'合成改名',english_name:'',selected:'false',photo_attachment_id:''});
  fs.writeFileSync(path.join(fixture,'card-edit-form.png'),(await window.webContents.capturePage()).toPNG());
  await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-form=card-edit]').requestSubmit()`);
  await waitFor(()=>Promise.resolve(projectControls.some(call=>call.action==='edit_employee')),'CARD_EDIT_NOT_SUBMITTED');
  assert.deepEqual(projectControls.find(call=>call.action==='edit_employee').arguments,{job_id:cardFixture.id,employee_id:employee,name:'合成改名',selected:false});
  await window.webContents.executeJavaScript(`(()=>{const card=document.querySelector('[data-megmeet-project-result=employee-cards]');card.querySelector('summary').click();card.querySelector('details').scrollIntoView({block:'center'});})()`);
  await waitFor(()=>window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('[data-megmeet-card-preview]')).some(img=>img.complete&&img.naturalWidth===1063&&img.naturalHeight===1673)`),'AUTHORIZED_CARD_PNG_NOT_LOADED');
  assert.ok(inlineRequests.every(call=>call.session_id===admitted.sessionId));
  fs.writeFileSync(path.join(fixture,'card-finished-preview.png'),(await window.webContents.capturePage()).toPNG());
  const costFixture=projectFixtures.find(card=>card.connector==='heatcost');
  const reopenCost=async mode=>{const label=costFixture.actions.find(action=>action.mode===mode).label;await window.webContents.executeJavaScript(`(()=>{const card=document.querySelector('[data-megmeet-project-result=heatcost]');card.scrollIntoView({block:'start'});Array.from(card.querySelectorAll('button')).find(el=>el.textContent===${JSON.stringify(label)}).click();})()`);};
  const controlBeforeClose=projectControls.length;
  for(const [mode,dismiss] of [['field','close'],['rate','escape'],['process','cancel']]){
    await reopenCost(mode);await setForm('[data-megmeet-project-form=cost-edit]',{key:mode==='field'?'length':mode==='rate'?'material':'cutting',value:mode==='process'?'true':'999'});
    if(dismiss==='escape'){window.webContents.sendInputEvent({type:'keyDown',keyCode:'Escape'});window.webContents.sendInputEvent({type:'keyUp',keyCode:'Escape'});}
    else await window.webContents.executeJavaScript(`(()=>{const f=document.querySelector('[data-megmeet-project-form=cost-edit]');${dismiss==='close'?"f.querySelector('[aria-label=关闭核对表单]').click()":"Array.from(f.querySelectorAll('button')).find(x=>x.textContent==='取消').click()"};})()`);
    await waitFor(()=>window.webContents.executeJavaScript(`!document.querySelector('[data-megmeet-project-form=cost-edit]')`),'COST_DISMISS_FAILED');
    await reopenCost(mode);assert.equal(await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-form=cost-edit] select[name=key]').value`),'');
    await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-form=cost-edit] [aria-label=关闭核对表单]').click()`);
  }
  assert.equal(projectControls.length,controlBeforeClose);
  for(const [mode,key,value,expected] of [['field','length','123','123'],['rate','material','42.5',42.5],['process','cutting','false',false]]){
    const label=costFixture.actions.find(action=>action.mode===mode).label;
    await window.webContents.executeJavaScript(`(()=>{const card=document.querySelector('[data-megmeet-project-result=heatcost]');card.scrollIntoView({block:'start'});Array.from(card.querySelectorAll('button')).find(el=>el.textContent===${JSON.stringify(label)}).click();})()`);
    await setForm('[data-megmeet-project-form=cost-edit]',{key,value});
    if(mode==='field'){await delay(200);const visible=await window.webContents.executeJavaScript(`(()=>{const close=document.querySelector('[data-megmeet-project-form=cost-edit] [aria-label=关闭核对表单]').getBoundingClientRect(),root=document.querySelector('[data-megmeet-project-results]').getBoundingClientRect();return close.top>=root.top&&close.bottom<=root.bottom;})()`);assert.equal(visible,true);fs.writeFileSync(path.join(fixture,'cost-edit-form.png'),(await window.webContents.capturePage()).toPNG());}
    await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-form=cost-edit]').requestSubmit()`);
    await waitFor(()=>Promise.resolve(projectControls.some(call=>call.action==='revise'&&call.arguments?.kind===mode)),'COST_EDIT_NOT_SUBMITTED');
    assert.deepEqual(projectControls.find(call=>call.action==='revise'&&call.arguments?.kind===mode).arguments,{analysis_id:costFixture.id,kind:mode,key,value:expected});
    await waitFor(()=>window.webContents.executeJavaScript(`!document.querySelector('[data-megmeet-project-form=cost-edit]')`),'COST_FORM_NOT_CLEARED');
  }
  assert.ok(projectControls.every(call=>call.session_id===admitted.sessionId&&call.path==='/__megmeet/tools/invoke'&&!call.has_password));
  const formControlCount=projectControls.length;assert.equal(formControlCount,4);
  fs.writeFileSync(path.join(fixture,'project-form-controls.json'),JSON.stringify({controls:projectControls,inlineRequests},null,2));
  server.projectJob=false;server.failedJob={status:'failed',job_id:'synthetic-monitor-failure',connector:'infrastructure-monitor',detail:'monitor_upstream_access_denied'};
  await waitFor(()=>window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-job=synthetic-monitor-failure]')?.innerText.includes('安全网关权限')`),'ASYNC_MONITOR_FAILURE_NOT_READABLE');
  const controlsBeforeDismiss=projectControls.length;await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-project-job=synthetic-monitor-failure] button').click()`);
  await waitFor(()=>window.webContents.executeJavaScript(`!document.querySelector('[data-megmeet-project-job=synthetic-monitor-failure]')`),'FAILURE_DISMISS_FAILED');assert.equal(projectControls.length,controlsBeforeDismiss);server.failedJob=null;
  server.showResults=false;
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-selected-skill=meeting] button").click()');await waitFor(()=>Promise.resolve(server.selected.length===0),'MEETING_REMOVAL_FAILED');
  await openSkills();await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-plugin-key=infrastructure-monitor]").click()');await waitFor(()=>Promise.resolve(server.selected.includes('infrastructure-monitor')),'VMWARE_SELECTION_NOT_STORED');
  await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-selected-skill=infrastructure-monitor]")'),'VMWARE_CHIP_NOT_RENDERED');
  assert.match(await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-selected-skill=infrastructure-monitor]").innerText'),/vmware[\s\S]*发送后执行/);
  assert.equal(promptBodies.length,1);assert.equal(projectControls.length,formControlCount);
  await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-selected-skill=infrastructure-monitor] button").click()');await waitFor(()=>Promise.resolve(server.selected.length===0),'VMWARE_REMOVAL_FAILED');
  server.rejectPrompt=true;
  // Reproduce the light toast surface used by the company custom theme.
  await window.webContents.executeJavaScript(`document.documentElement.style.setProperty('--dsw-alias-toast-bg','#ffffff')`);
  await window.webContents.executeJavaScript(`document.querySelector('[contenteditable=true]').focus()`);await window.webContents.insertText('SYNTHETIC_REJECTED_DRAFT');
  await window.webContents.executeJavaScript(`document.querySelector('button[aria-label=发送消息]').click()`);
  await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('[data-megmeet-composer-feedback]')`),'READABLE_REJECTION_MISSING');
  const feedback=await window.webContents.executeJavaScript(`(()=>{const el=document.querySelector('[data-megmeet-composer-feedback]'),css=getComputedStyle(el);return{text:el.innerText,color:css.color,bg:css.backgroundColor};})()`);
  assert.match(feedback.text,/输入内容已保留/);assert.equal(feedback.color,'rgb(255, 255, 255)');assert.equal(feedback.bg,'rgb(38, 61, 54)');
  await waitFor(()=>window.webContents.executeJavaScript(`document.querySelector('[contenteditable=true]').innerText==='SYNTHETIC_REJECTED_DRAFT'`),'REJECTED_DRAFT_NOT_RESTORED');
  const rejectedCount=promptBodies.length;await delay(500);assert.equal(promptBodies.length,rejectedCount);
  fs.writeFileSync(path.join(fixture,'readable-send-error.png'),(await window.webContents.capturePage()).toPNG());
  await window.webContents.executeJavaScript(`document.querySelector('[data-megmeet-composer-feedback] [aria-label=关闭提示]').click()`);
  assert.equal(await window.webContents.executeJavaScript(`!!document.querySelector('[data-megmeet-composer-feedback]')`),false);
  await openSkills();await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-dingtalk-manage]").click()');await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-dingtalk-cli]")'),'CLI_PANEL_READY');
  assert.equal(await window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-mcp-form]")'),false);
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[data-megmeet-dingtalk-cli] button")).find(b=>b.textContent==="连接钉钉").click()');await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-dingtalk-authorize]")'),'CLI_AUTH_LINK');
  server.cliState='ready';await waitFor(()=>window.webContents.executeJavaScript('document.querySelector("[data-megmeet-dingtalk-cli]").textContent.includes("已连接")'),'CLI_AUTH_COMPLETED');
  const mcpResult={official_cli_panel:true,no_mcp_secret_input:true,device_link:true,poll_ready:true};
  await window.webContents.executeJavaScript('document.querySelector("[aria-label=关闭技能选择]").click()');
  server.toolPending=[{confirmation_id:'synthetic-cli-write',plugin_name:'钉钉',preview:{provider:'dingtalk_workspace_cli',summary:'创建合成测试待办',account:{name:'合成测试员工',organization:'合成测试组织',profile:'owner-test'},arguments:{title:'SYNTHETIC_TODO'}}}];
  await waitFor(()=>window.webContents.executeJavaScript('document.querySelector("[data-megmeet-confirm-account]")?.textContent.includes("合成测试员工 · 合成测试组织")'),'CLI_CONFIRM_ACCOUNT_NOT_VISIBLE');
  assert.equal(server.confirmCalls||0,0);
  await window.webContents.executeJavaScript('Array.from(document.querySelectorAll("[data-megmeet-tool-confirmations] button")).find(b=>b.textContent==="取消").click()');
  await waitFor(()=>Promise.resolve(server.confirmCalls===1),'CLI_CANCEL_NOT_SENT');
  mcpResult.write_preview_account_organization=true;
  mcpResult.no_automatic_write=true;
  nativeTheme.themeSource='dark';await waitFor(()=>window.webContents.executeJavaScript('document.body.hasAttribute("data-ds-dark-theme")'),'NATIVE_DARK_NOT_SET');
  await openSkills();await window.webContents.executeJavaScript('document.querySelector("[data-megmeet-dingtalk-manage]").click()');await waitFor(()=>window.webContents.executeJavaScript('!!document.querySelector("[data-megmeet-dingtalk-cli] button:not(:disabled)")'),'DARK_MCP_READY');const darkPanel=await window.webContents.executeJavaScript('getComputedStyle(document.querySelector("[data-megmeet-plugin-popover]")).backgroundColor');assert.notEqual(darkPanel,'rgb(255, 255, 255)');fs.writeFileSync(path.join(fixture,'workflow-skills-dark.png'),(await window.webContents.capturePage()).toPNG());
  window.setSize(420,900);await delay(500);
  const narrow=await window.webContents.executeJavaScript('(()=>{const p=document.querySelector("[data-megmeet-plugin-popover]").getBoundingClientRect(),f=document.querySelector("[data-megmeet-plugin-popover] footer").getBoundingClientRect();return {left:p.left,right:p.right,width:innerWidth,footerVisible:f.bottom<=p.bottom&&f.top>=p.top,workflowBadges:document.querySelectorAll("[data-megmeet-workflow-badge]").length};})()');assert.ok(narrow.left>=0&&narrow.right<=narrow.width);assert.equal(narrow.footerVisible,true);fs.writeFileSync(path.join(fixture,'workflow-skills-narrow.png'),(await window.webContents.capturePage()).toPNG());
  assert.deepEqual(errors,[]);
  assert.ok(selectionRequests.every(x=>x.session_id===admitted.sessionId));
  const result={schema:'project-workflow-ui-native-v1',status:'PASS',client:fixtureClient,fixture,company_connection:false,runtime:'0.2.0-rc.2',checks:{four_project_labels:directory.filter(x=>x.workflow).map(x=>x.key),vmware_display:true,vmware_internal_key_preserved:true,other_skills_model_optional:true,selection_does_not_execute:true,selection_does_not_modify_draft:true,native_prompt_accepted:true,native_attachment_receipt:true,selected_session_matches_prompt:true,request_id_present:true,project_cards_from_state_rendered:true,meeting_download_callback:true,card_human_edit:true,card_generated_png_inline:true,cost_three_human_forms:true,form_values_typed:true,form_session_bound:true,cost_close_cancel_escape:true,cost_close_no_submit:true,async_failure_readable_dismissible:true,rejected_send_readable:true,rejected_send_preserves_draft:true,no_automatic_resubmit:true,selection_removed:true,native_dark:true,narrow},boundary:'Actual native UI/upload/prompt/response with synthetic catalog, model and actual business-generated synthetic project cards. Worker workflow execution and ACL not tested here.',errors,warnings};
  result.checks.dingtalk_cli=mcpResult;
  fs.writeFileSync(path.join(here,fixtureClient==='desktop'?'browser-desktop-result.json':'browser-result.json'),JSON.stringify(result,null,2)+'\n');fs.writeFileSync(path.join(fixture,'result.json'),JSON.stringify(result,null,2)+'\n');code=0;

 }catch(error){fs.writeFileSync(path.join(fixture,'failure.txt'),String(error.stack));if(window&&!window.isDestroyed()){fs.writeFileSync(path.join(fixture,'failed.png'),(await window.webContents.capturePage()).toPNG());fs.writeFileSync(path.join(fixture,'failed.json'),JSON.stringify({requests,errors,html:await window.webContents.executeJavaScript('document.body.innerHTML')},null,2));}}
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
  }catch(error){code=1;fs.writeFileSync(path.join(fixture,'cleanup-failure.json'),JSON.stringify({reason:'FIXTURE_CLEANUP_FAILED'}));}
  finally{cleanupStep('APP_EXIT');app.exit(code);}
 }
});
