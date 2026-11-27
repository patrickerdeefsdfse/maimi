// Offline synthetic Host rehearsal. Never loads employee DSH_HOME or credentials.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import net from 'node:net';
import { spawn, execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const runtime = process.env.OFFICE_PROBE_RUNTIME || path.resolve(here, '../../../desktop-local/runtime');
const owned = fs.mkdtempSync(path.join(os.tmpdir(), 'megmeet-office-native-'));
const staging = path.join(owned, 'runtime'), adapter = path.join(staging, 'office-adapter');
fs.mkdirSync(adapter, { recursive: true });
fs.symlinkSync(path.join(runtime, 'node_modules'), path.join(staging, 'node_modules'), process.platform === 'win32' ? 'junction' : 'dir');
for (const name of ['index.mjs', 'adapter.mjs', 'transport.mjs']) fs.copyFileSync(path.join(here, name), path.join(adapter, name));
const report = path.join(owned, 'report.json');
const workspace = path.join(owned, 'workspace'); fs.mkdirSync(workspace);
fs.writeFileSync(path.join(adapter, 'synthetic-probe.mjs'), `
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
export const inject=['tools','fs','sessions','sessionQuery','sandboxPolicy','attachments'];
export function apply(ctx, config) {
  void (async () => {
    await new Promise(resolve => setTimeout(resolve, 300));
    const checks=[];
    try {
      for (const name of ['office_document','office_capabilities','office_attachments']) assert.ok(ctx.tools.get(name),name);
      checks.push('REAL_NATIVE_TOOL_REGISTRATION');
      const session=ctx.sessions.create('office-native-probe-a',{meta:{cwd:config.workspace}});
      session.append('sandbox/mode',{mode:'workspace-write'});
      const agent={session}, signal=new AbortController().signal;
      const invoke=async(name,args,owner=agent)=>ctx.tools.execute({name,arguments:args,agent:owner,signal,callId:'office-'+checks.length});
      const good=async(name,args)=>{const r=await invoke(name,args); assert.equal(r.isError,false,JSON.stringify(r)); return r.value.result;};
      const capabilities=await good('office_capabilities',{}); assert.ok(capabilities.read_formats.includes('docx'));
      checks.push('NATIVE_PIPELINE_ENGINE_CAPABILITIES');
      await good('office_document',{operation:'create',output:'original.docx',options:{paragraphs:['Native synthetic 中文文档','Unchanged original']}});
      const before=await fs.readFile(path.join(config.workspace,'original.docx'));
      const attachment=await ctx.attachments.saveFile({data:before,name:'真实附件.docx'});
      session.append('user/message',createUserMessage({source:{kind:'user'},content:[{type:'file',attachment}]}),{surfaceOp:'append'});
      const listed=await good('office_attachments',{}); assert.equal(listed.attachments[0].attachment_id,attachment.attachmentId);
      const read=await good('office_document',{operation:'read',attachment_id:attachment.attachmentId});
      assert.match(JSON.stringify(read),/Native synthetic 中文文档/);
      assert.equal((await fs.readdir(config.workspace)).filter(n=>n.startsWith('.megmeet-office-')).length,0);
      checks.push('REAL_NATIVE_STORED_ATTACHMENT_READ_AND_TEMP_CLEANUP');
      const other=ctx.sessions.create('office-native-probe-b',{meta:{cwd:config.workspace}});
      const denied=await invoke('office_document',{operation:'read',attachment_id:attachment.attachmentId},{session:other});
      assert.equal(denied.isError,true); assert.match(JSON.stringify(denied),/OWN_ATTACHMENT/);
      checks.push('OTHER_SESSION_ATTACHMENT_DENIED');
      session.append('sandbox/mode',{mode:'read-only'});
      const blocked=await invoke('office_document',{operation:'edit',input:'original.docx',output:'readonly.docx',options:{replacements:[{find:'original',replace:'changed'}]}});
      assert.equal(blocked.isError,true); assert.match(JSON.stringify(blocked),/READ_ONLY/);
      assert.equal((await fs.readdir(config.workspace)).includes('readonly.docx'),false);
      checks.push('NATIVE_READ_ONLY_SANDBOX_DENIED');
      session.append('sandbox/mode',{mode:'workspace-write'});
      await good('office_document',{operation:'edit',input:'original.docx',output:'edited.docx',options:{replacements:[{find:'Unchanged original',replace:'New copy'}]}});
      assert.deepEqual(await fs.readFile(path.join(config.workspace,'original.docx')),before);
      checks.push('NATIVE_WORKSPACE_WRITE_NEW_COPY_ORIGINAL_UNCHANGED');
      const stop=ctx.on('tools/pre-execute',(exec,next)=>exec.name==='office_document'?{kind:'deny',reason:'SYNTHETIC_APPROVAL_DENIED'}:next());
      const policyBlocked=await invoke('office_document',{operation:'create',output:'policy-blocked.txt',options:{text:'should not exist'}});
      assert.equal(policyBlocked.isError,true); assert.match(JSON.stringify(policyBlocked),/SYNTHETIC_APPROVAL_DENIED/);
      assert.equal((await fs.readdir(config.workspace)).includes('policy-blocked.txt'),false); stop();
      checks.push('NATIVE_PRE_EXECUTE_DENIAL_NOT_BYPASSED');
      if(config.fullFormats){
        await good('office_document',{operation:'convert',input:'original.docx',output:'native-converted.pdf'});
        assert.match(JSON.stringify(await good('office_document',{operation:'read',input:'native-converted.pdf'})),/Native synthetic/);
        checks.push('LIVE_NATIVE_HOST_LIBREOFFICE_CONVERSION');
        assert.match(JSON.stringify(await good('office_document',{operation:'read',input:'ocr.png'})),/123/);
        checks.push('LIVE_NATIVE_HOST_OFFLINE_OCR');
      }
      await fs.writeFile(config.report,JSON.stringify({ok:true,checks},null,2));
    } catch(error) { await fs.writeFile(config.report,JSON.stringify({ok:false,checks,error:String(error.stack).slice(0,5000)},null,2)); }
  })();
}
`);
const config = { engineRoot: process.env.OFFICE_PROBE_ENGINE || path.resolve(here, '../../../office-engine'),
  pythonExecutable: process.env.OFFICE_PROBE_PYTHON || path.resolve(here, '../../../office-engine/runtime-win/python/python.exe') };
const fullFormats=process.env.OFFICE_PROBE_FULL_FORMATS==='1';
if(fullFormats)execFileSync(config.pythonExecutable,['-I','-c',
  "import sys;from PIL import Image,ImageDraw,ImageFont;im=Image.new('RGB',(800,180),'white');font=ImageFont.truetype(sys.argv[2],48);ImageDraw.Draw(im).text((20,50),'MEGMEET OFFICE 123',font=font,fill='black');im.save(sys.argv[1])",
  path.join(workspace,'ocr.png'),process.platform==='win32'?'C:/Windows/Fonts/arial.ttf':'/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'],{windowsHide:true,stdio:'pipe',timeout:30000});
const patch = path.join(owned, 'office.patch.json');
fs.writeFileSync(patch, JSON.stringify([{ insert: [
  { id: 'megmeet-office-adapter', name: pathToFileURL(path.join(adapter, 'index.mjs')).href, config },
  { id: 'megmeet-office-native-probe', name: pathToFileURL(path.join(adapter, 'synthetic-probe.mjs')).href, config: { workspace, report, fullFormats } },
] }]));
const port = await new Promise(resolve => { const server=net.createServer(); server.listen(0,'127.0.0.1',()=>{const p=server.address().port;server.close(()=>resolve(p));}); });
for (const name of ['home','dsh','appdata','localappdata']) fs.mkdirSync(path.join(owned,name));
const env={ PATH:path.dirname(config.pythonExecutable),SystemRoot:process.env.SystemRoot,WINDIR:process.env.WINDIR,TEMP:os.tmpdir(),TMP:os.tmpdir(),
  HOME:path.join(owned,'home'),USERPROFILE:path.join(owned,'home'),APPDATA:path.join(owned,'appdata'),LOCALAPPDATA:path.join(owned,'localappdata'),
  DSH_HOME:path.join(owned,'dsh'),DSH_TELEMETRY_DISABLED:'1' };
const child=spawn(process.env.OFFICE_PROBE_NODE || process.execPath,['--expose-internals','--import',pathToFileURL(process.env.OFFICE_PROBE_GUARD || path.join(here,'../community-plugins-network-guard.mjs')).href,
  path.join(runtime,'node_modules/@deepseek-ai/dsh/lib/bin.js'),'--profile','web','--patch',patch,'--host','127.0.0.1','--port',String(port),'--no-open'],
  {cwd:staging,env,windowsHide:true,detached:process.platform!=='win32',stdio:['ignore','pipe','pipe']});
let logs=''; for(const stream of [child.stdout,child.stderr])stream.on('data',value=>{logs=(logs+value).slice(-65536);});
try {
  const deadline=Date.now()+150000;
  while(!fs.existsSync(report)&&Date.now()<deadline&&child.exitCode===null)await new Promise(r=>setTimeout(r,500));
  if(!fs.existsSync(report))throw Error('Native probe did not finish: '+logs.replace(/token=[A-Za-z0-9_-]+/g,'token=[synthetic-redacted]').slice(-8000));
  const result=JSON.parse(fs.readFileSync(report,'utf8'));
  console.log(JSON.stringify({...result,scope:'Temporary offline Host, real native tools/session/attachments, real local Python engine; no model or company calls',evidence:report},null,2));
  if(!result.ok)process.exitCode=1;
}finally{
  if(child.exitCode===null){
    if(process.platform==='win32')try{execFileSync(path.join(process.env.SystemRoot,'System32/taskkill.exe'),['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore',timeout:10000});}catch{child.kill();}
    else { try { process.kill(-child.pid,'SIGTERM'); } catch { child.kill(); }
      await Promise.race([new Promise(resolve=>child.once('close',resolve)),new Promise(resolve=>setTimeout(resolve,3000))]);
      if(child.exitCode===null)try{process.kill(-child.pid,'SIGKILL');}catch{child.kill('SIGKILL');}
    }
  }
}
