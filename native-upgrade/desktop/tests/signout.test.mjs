import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {execFileSync} from 'node:child_process';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(new URL('../../desktop/package.json',import.meta.url)),ts=require('typescript');
const transpile=source=>{const result=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2024,module:ts.ModuleKind.CommonJS},reportDiagnostics:true});assert.equal(result.diagnostics.length,0,result.diagnostics.map(d=>ts.flattenDiagnosticMessageText(d.messageText,' ')).join('; '));return result.outputText;};
const loaded={exports:{}};
vm.runInNewContext(transpile(fs.readFileSync(new URL('../src/company-signout.ts',import.meta.url),'utf8')),{module:loaded,exports:loaded.exports});
const {CompanySignOutController}=loaded.exports;
function fixture(overrides={}){
 const events=[];let current;
 const target={identity:{},lock:async()=>events.push('lock'),unlock:async()=>events.push('unlock'),blockResume:async()=>events.push('block'),stop:async()=>events.push('stop'),revoke:async()=>{events.push('revoke');return true;}};
 current=target;const options={current:()=>current,confirmAccount:async()=>{events.push('account');return true;},confirmTasks:async()=>{events.push('tasks');return true;},notify:async result=>events.push(result),finish:()=>events.push('finish'),...overrides};
 return {events,target,options,setCurrent:value=>current=value,controller:new CompanySignOutController(options)};
}
test('sign-out locks task admission, confirms tasks, blocks resume, stops before revoking',async()=>{
 const f=fixture();assert.equal(await f.controller.run(),true);assert.deepEqual(f.events,['account','lock','tasks','block','stop','revoke','finish']);assert.equal(await f.controller.run(),false);assert.equal(f.events.filter(v=>v==='revoke').length,1);
});
test('cancel at account prompt leaves tasks and credentials intact',async()=>{
 const f=fixture({confirmAccount:async()=>false});assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,[]);
});
test('cancel native task confirmation unlocks without stopping or revoking',async()=>{
 const f=fixture({confirmTasks:async()=>false});assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,['account','lock','unlock']);
});
test('late host replacement cannot log out the replacement account',async()=>{
 const f=fixture();f.options.confirmTasks=async()=>{f.events.push('tasks');f.setCurrent({identity:{}});return true;};assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,['account','lock','tasks','unlock']);
});
test('failed lock best-effort unlocks and does not revoke',async()=>{
 const f=fixture();f.target.lock=async()=>{f.events.push('lock');throw Error('synthetic');};assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,['account','lock','not-completed','unlock']);
});
test('failed durable block leaves session running and unlocks',async()=>{
 const f=fixture();f.target.blockResume=async()=>{f.events.push('block');throw Error('synthetic');};assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,['account','lock','tasks','block','not-completed','unlock']);
});
test('failed Host stop preserves no-resume and closes without revocation or auto login',async()=>{
 const f=fixture();f.target.stop=async()=>{f.events.push('stop');throw Error('synthetic');};assert.equal(await f.controller.run(),false);assert.deepEqual(f.events,['account','lock','tasks','block','stop','stop-failed','finish']);
});
test('offline remote revocation keeps local block and exits',async()=>{
 const f=fixture();f.target.revoke=async()=>{f.events.push('revoke');throw Error('offline');};assert.equal(await f.controller.run(),true);assert.deepEqual(f.events,['account','lock','tasks','block','stop','revoke','revocation-pending','finish']);
});
test('concurrent menu clicks join one confirmation',async()=>{
 const f=fixture();let release;f.options.confirmAccount=()=>new Promise(resolve=>{release=resolve;});const one=f.controller.run(),two=f.controller.run();assert.equal(one,two);release(true);assert.equal(await one,true);assert.equal(f.events.filter(v=>v==='lock').length,1);
});
test('notification failure still exits after local sign-out commitment',async()=>{
 const f=fixture({notify:async()=>{throw Error('dialog failed');}});f.target.revoke=async()=>false;assert.equal(await f.controller.run(),true);assert.equal(f.events.at(-1),'finish');
});
test('actual transformed upstream application menu invokes the company coordinator through existing native route',async()=>{
 const main=execFileSync('python',[fileURLToPath(new URL('../scripts/refresh-patch.py',import.meta.url)),'--emit-main'],{encoding:'utf8',maxBuffer:2*1024*1024});
 assert.ok(main.includes("assertDesktopSender(event, ['app'])"));assert.ok(main.includes('event.senderFrame !== mainWindow.webContents.mainFrame'));
 const menu=main.slice(main.indexOf('  const applicationItems ='),main.indexOf('  const devToolsItems:'));
 const binding=main.slice(main.indexOf('  const companySignOut ='),main.indexOf("  if (process.platform === 'win32') {\n    ipcMain.handle(DESKTOP_IPC.windowsMenu,"));
 const calls=[];const company={blockResume:async()=>calls.push('block'),signOut:async()=>{calls.push('revoke');return true;}};
 const context={CompanySignOutController,COMPANY_CANDIDATE:true,companyActiveHost:company,backend:{host:{updateTasks:async action=>calls.push(action)},close:async()=>calls.push('backend-close')},platformView:{closeAndWait:async()=>calls.push('platform-close')},quitConfirmation:{confirm:async()=>{calls.push('native-task-confirm');return true;}},dialog:{showMessageBox:async()=>({response:0})},quitWithoutConfirmation:()=>calls.push('quit'),quitting:false,shellInstallerOwnsQuit:false,process:{platform:'win32'},development:false,darwin:false,hideCommands:[],currentDesktopLocale:()=>({messages:{}}),showAbout:async()=>{},openUpdatePrompt:async()=>{},commandManager:{show:async()=>{}},requestCompanySignOut:undefined};
 vm.createContext(context);vm.runInContext(transpile(binding+'\n'+menu+'\nglobalThis.items=applicationItems();'),context);
 const item=context.items.find(value=>value.label==='退出公司登录并关闭麦米');assert.ok(item);assert.equal(item.enabled,true);item.click();await context.requestCompanySignOut();assert.deepEqual(calls,['lock','native-task-confirm','block','platform-close','backend-close','revoke','quit']);
});
