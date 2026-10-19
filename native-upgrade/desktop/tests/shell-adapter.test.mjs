import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const ts=require('typescript');
const source=fs.readFileSync(new URL('../src/company-shell-host.ts',import.meta.url),'utf8');
function load({adapter,response=0}={}){
 const calls={browser:[],dialogs:[]};
 const electron={app:{getAppPath:()=>'/synthetic-app',getPath:()=>'/synthetic-state'},dialog:{showMessageBox:async value=>{calls.dialogs.push(value);return {response};}},shell:{openExternal:async url=>calls.browser.push(url)}};
 const translated=ts.transpileModule(source.replace('await import(entry)','await loadAdapter(entry)'),{compilerOptions:{target:ts.ScriptTarget.ES2024,module:ts.ModuleKind.CommonJS},reportDiagnostics:true});
 assert.equal(translated.diagnostics.length,0);
 const module={exports:{}};
 vm.runInNewContext(translated.outputText,{module,exports:module.exports,require:name=>name==='electron'?electron:require(name),loadAdapter:async()=>adapter,URL,AbortController});
 return {...module.exports,calls};
}
test('official shell adapter forwards Native HostReady and task controls without account data',async()=>{
 let options,stopped=0;const fake={url:'http://127.0.0.1:32123/?token=synthetic',injections:[],stop:async()=>stopped++,inspectQuit:async()=>({activeTasks:true,scheduledTasks:false}),updateTasks:async action=>action==='lock'};
 const api=load({adapter:{signInAndStartCompanyNativeSession:async value=>{options=value;return fake;}}});
 const host=new api.CompanyNativeDesktopHost('/runtime',()=>{},'/primary-runtime',{pnpm:'/pnpm.mjs',nodeBin:'/bin'});
 assert.equal((await host.start()).url,fake.url);assert.equal(options.runtimeRoot,'/runtime');assert.ok(options.stateRoot.endsWith('native-company-state'));
 assert.equal(options.primaryRuntime,'/primary-runtime');
 assert.equal(options.packageManager.pnpm,'/pnpm.mjs');
 assert.equal((await host.inspectQuit()).activeTasks,true);assert.equal(await host.updateTasks('lock'),true);
 await options.openBrowser('https://platform-ai.megmeet.com/v1/desktop/auth/authorize?flow=synthetic',{verificationCode:'ABCD-1234'});
 assert.equal(api.calls.dialogs[0].detail,'ABCD-1234');assert.equal(api.calls.browser.length,1);
 await assert.rejects(options.openBrowser('https://evil.invalid/v1/desktop/auth/authorize',{verificationCode:'ABCD-1234'}),/COMPANY_AUTH_URL_INVALID/);
 await host.stop();assert.equal(stopped,1);assert.equal(options.signal.aborted,true);
});
test('company browser login cancellation does not open external browser',async()=>{
 let options;const api=load({response:1,adapter:{signInAndStartCompanyNativeSession:async value=>{options=value;return {url:'http://127.0.0.1:1',injections:[]};}}});
 await new api.CompanyNativeDesktopHost('/runtime',()=>{}).start();
 await assert.rejects(options.openBrowser('https://platform-ai.megmeet.com/v1/desktop/auth/authorize',{verificationCode:'ABCD-1234'}),/COMPANY_LOGIN_CANCELLED/);
 assert.equal(options.signal.aborted,true);assert.equal(api.calls.browser.length,0);
});
test('cloud access uses a declared native sidebar slot and contains no authentication values',()=>{
 let registered,component;
 const context={slots:{inject:(slot,callback)=>{assert.equal(slot,'sidebar.footer.action');return callback();},register:(row,fn)=>{registered=row;component=fn;}}};
 vm.runInNewContext(fs.readFileSync(new URL('../src/native-desktop-ui/client.js',import.meta.url),'utf8'),{window:{__ModuleLoader__:{load:definition=>definition.factory(()=>({createElement:(tag,props,children)=>({tag,props,children})})).apply(context)}}});
 assert.equal(registered.id,'maimi-cloud-entry');const value=component({wide:true});
 assert.equal(value.props.href,'https://platform-ai.megmeet.com/maimi/web/chat');assert.equal(value.props.target,'_blank');assert.match(value.props.rel,/noopener/);
});
test('staged native main connects company host and disables official update/account startup',()=>{
 assert.ok(!fs.readFileSync(new URL('../patches/native-shell.patch',import.meta.url),'utf8').includes('\r'));
 const main=fs.readFileSync(new URL('../native-source/apps/desktop/src/main.ts',import.meta.url),'utf8');
 assert.match(main,/new CompanyNativeDesktopHost\(resources\.dsh, onFailure, primaryRuntime, resources\)/);
 assert.match(main,/COMPANY_CANDIDATE \? \(\) => false : undefined/);
 assert.match(main,/resolveDesktopPolicyConfig\(COMPANY_CANDIDATE \? undefined : policyInput/);
 assert.match(main,/COMPANY_ACCOUNT_MANAGED_BY_IAM/);
 const parsed=ts.transpileModule(main,{compilerOptions:{target:ts.ScriptTarget.ES2024,module:ts.ModuleKind.ESNext},reportDiagnostics:true});
 assert.equal(parsed.diagnostics.length,0);
});
