import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {createRequire} from 'node:module';
const require=createRequire(new URL('../../desktop/package.json',import.meta.url));
const ts=require('typescript');
test('development payload follows the official win-x64 target resolver even on arm64 host',async()=>{
 const paths=await import('../../upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317/apps/desktop/scripts/desktop-build-paths.mjs');
 const resolved=paths.developmentRuntimeDirectory({},'win32','arm64').replaceAll('\\','/');assert.ok(resolved.endsWith('/apps/desktop/.desktop-build/targets/win-x64/runtime/primary-runtime'));
 const script=fs.readFileSync(new URL('../scripts/prepare-native-shell.py',import.meta.url),'utf8');assert.ok(!script.includes("join(app.getAppPath(), '.desktop-build', 'primary-runtime')"));
 const patch=fs.readFileSync(new URL('../patches/native-shell.patch',import.meta.url),'utf8');assert.ok(!patch.includes('-    ? developmentPrimaryRuntime()'));assert.ok(!patch.includes("join(app.getAppPath(), '.desktop-build', 'primary-runtime')"));
});
test('real shell adapter forwards Electron safeStorage solely to parent session adapter',async()=>{
 const source=fs.readFileSync(new URL('../src/company-shell-host.ts',import.meta.url),'utf8');
 const safeStorage={isEncryptionAvailable:()=>true},calls={};
 const fake={url:'http://127.0.0.1:31111/?token=synthetic',injections:[],stop:async()=>calls.stopped=true,signOut:async()=>{calls.loggedOut=true;return false;},inspectQuit:async()=>({activeTasks:false,scheduledTasks:false}),updateTasks:async()=>false};
 const electron={safeStorage,app:{getAppPath:()=>'/synthetic-app',getPath:()=>'/synthetic-state'},dialog:{showMessageBox:async()=>({response:0})},shell:{openExternal:async()=>{}}};
 const translated=ts.transpileModule(source.replace('await import(entry)','await loadAdapter(entry)'),{compilerOptions:{target:ts.ScriptTarget.ES2024,module:ts.ModuleKind.CommonJS},reportDiagnostics:true});assert.equal(translated.diagnostics.length,0);
 const module={exports:{}};vm.runInNewContext(translated.outputText,{module,exports:module.exports,require:name=>name==='electron'?electron:require(name),loadAdapter:async()=>({signInAndStartCompanyNativeSession:async options=>{calls.options=options;return fake;}}),URL,AbortController});
 const host=new module.exports.CompanyNativeDesktopHost('/runtime',()=>{});const ready=await host.start();assert.equal(calls.options.safeStorage,safeStorage);assert.deepEqual(Object.keys(ready).sort(),['injections','url']);assert.equal(await host.signOut(),false);assert.equal(calls.loggedOut,true);assert.equal(calls.options.signal.aborted,true);
});
