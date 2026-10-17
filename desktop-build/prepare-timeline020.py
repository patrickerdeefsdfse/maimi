from pathlib import Path
import json,shutil
root=Path(__file__).resolve().parent
repo=root.parents[2]
old=repo/'output/remaining-fixes-r30-20260928/search-ui/ui'
large=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')
s=(old/'test-timeline-browser.cjs').read_text(encoding='utf-8')
s=s.replace("runtime=path.resolve(here,'../../../dsh-rc2-implementation-20260928/ui'),area=path.join(here,'ui-browser-fixtures')",'runtime='+json.dumps(str(large))+",area=path.join(runtime,'ui-browser-fixtures')")
s=s.replace("runtime:'0.1.7-rc.2'", "runtime:'0.2.0-rc.2'")
s=s.replace("[{id:'llm-deepseek',disabled:true},{id:'llm-deepseek-account',disabled:true},{id:'session-log-deepseek',config:{enabled:false}},{id:'session-telemetry-otel',config:{mode:'DISABLED'}},{id:'skill-office',config:{cli:false}}]", "(await import(require('node:url').pathToFileURL(path.join(here,'app/enterprise-profile.mjs')))).enterpriseProfile({cloud:false})")
s=s.replace("path.join(['dsh-client-ui-brand-official','dsh-client-ui-chat'].includes(name)?here:runtime,'overlays'", "path.join(here,'overlays'")
s=s.replace("path.join(runtime,'synthetic.xlsx')","path.join(here,'synthetic.xlsx')")
s=s.replace("path.join(runtime,'app/catalog.mjs')","path.join(here,'app/catalog.mjs')")
s=s.replace("  const fixturePatch=path.join(fixture,'profile.patch.json');", "  const profileDir=path.join(fixture,'state/profiles/web');fs.mkdirSync(profileDir,{recursive:true});fs.writeFileSync(path.join(profileDir,'package.json'),JSON.stringify({name:'synthetic-profile',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}}));await (await import(require('node:url').pathToFileURL(path.join(here,'app/community-profile.mjs')))).prepareCommunityProfile({home:path.join(fixture,'state'),runtimeRoot:path.join(runtime,'runtime-win32-x64'),yaml:require(path.join(runtime,'runtime-win32-x64/node_modules/yaml'))});\n  const fixturePatch=path.join(fixture,'profile.patch.json');")
s=s.replace("assert.deepEqual(errors,[]);", "await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()==='Token 总览')?.click()`);await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('nav[aria-label=\"Token 总览\"]')`),'TOKENMETER_CENTER_READY');fs.writeFileSync(path.join(fixture,'tokenmeter020-center.png'),(await window.webContents.capturePage()).toPNG());await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('nav[aria-label=\"Token 总览\"] button')).find(b=>b.textContent.includes('设置'))?.click()`);await delay(500);fs.writeFileSync(path.join(fixture,'tokenmeter020-settings.png'),(await window.webContents.capturePage()).toPNG());\n  assert.deepEqual(errors,[]);")
s=s.replace("const inlineRequests=[];", "for(const card of projectFixtures)if(card.connector==='infrastructure-monitor')for(const action of card.actions||[])if(action.form==='monitor-login'){action.fields=[{name:'location',type:'select',label:'地区',options:[{value:'synthetic',label:'合成地区',target:'192.0.2.10'}]},{name:'target',label:'IP',type:'text'},{name:'username',label:'账号'},{name:'password',label:'密码'}];}\nconst inlineRequests=[];")
s=s.replace("fs.writeFileSync(path.join(fixture,'tokenmeter020-center.png'),(await window.webContents.capturePage()).toPNG());", "fs.writeFileSync(path.join(fixture,'tokenmeter020-center.png'),(await window.webContents.capturePage()).toPNG());await window.webContents.executeJavaScript(`Array.from(document.querySelectorAll('button[title]')).find(b=>b.title.startsWith('弹出'))?.click()`);await waitFor(()=>window.webContents.executeJavaScript(`!!document.querySelector('[role=dialog][data-widget]')`),'TOKENMETER_FLOAT_READY');await delay(500);fs.writeFileSync(path.join(fixture,'tokenmeter020-float.png'),(await window.webContents.capturePage()).toPNG());")
s=s.replace("'synthetic-model'", "'deepseek-v4.1-flash'").replace("name:'Synthetic'", "name:'DeepSeek V4.1 Flash'")
s=s.replace(";modelSection.providers['synthetic-fixture']=modelSection.providers['deepseek-official'];delete modelSection.providers['deepseek-official'];", ";")
s=s.replace("provider:'synthetic-fixture'", "provider:'deepseek-official'")
s=s.replace("const fixtureFiles=path.resolve(here,'../../../workflow-native-20260925')", "const fixtureFiles="+json.dumps(str(repo/'output/workflow-native-20260925')))
s=s.replace("console.error(error.stack||error);", "")
s=s.replace("console.log(JSON.stringify({status:'PASS',fixture}));", "")
s=s.replace("console.error(error.stack);", "")
s=s.replace("console.error('FIXTURE_CLEANUP_FAILED:'+error.message);", "fs.writeFileSync(path.join(fixture,'cleanup-failure.json'),JSON.stringify({status:'FAIL',reason:'FIXTURE_CLEANUP_FAILED',error:String(error.message).slice(0,500)}));")
# Diagnostics are file based; broken inherited console pipes do not obscure
# the original fixture result or launch an Electron main-process dialog.
anchor="const rawLog=path.join(fixture,'native.raw.log');"
inject="""
for(const stream of [process.stdout,process.stderr])stream?.on('error',error=>{
 if(error.code==='EPIPE'){fs.writeFileSync(path.join(fixture,'diagnostic-pipe-closed.json'),JSON.stringify({code:'EPIPE',test_result_preserved:true}));return;}
 void fatalFixture(error);
});
const bounded=work=>Promise.race([work,new Promise((_,reject)=>setTimeout(()=>reject(Error('FIXTURE_DIAGNOSTIC_TIMEOUT')),3000))]);
"""
s=s.replace(anchor,anchor+inject)
oldcatch="if(window&&!window.isDestroyed()){fs.writeFileSync(path.join(fixture,'failed.png'),(await window.webContents.capturePage()).toPNG());fs.writeFileSync(path.join(fixture,'failed.json'),JSON.stringify({requests,errors,html:await window.webContents.executeJavaScript('document.body.innerHTML')},null,2));}"
s=s.replace(oldcatch,"fs.writeFileSync(path.join(fixture,'failure.json'),JSON.stringify({status:'FAIL',phase,reason:String(error.message).slice(0,500)}));try{if(window&&!window.isDestroyed()){fs.writeFileSync(path.join(fixture,'failed.png'),(await bounded(window.webContents.capturePage())).toPNG());fs.writeFileSync(path.join(fixture,'failed.json'),JSON.stringify({requests,errors,html:await bounded(window.webContents.executeJavaScript('document.body.innerHTML'))},null,2));}}catch{}");
(root/'test-timeline-browser.cjs').write_text(s,encoding='utf-8')
for name in ('synthetic-result.zip',):shutil.copyfile(old/name,root/name)
# Retire the obsolete fixture safely too; never leave its console-based catch.
prior=root/'test-workflow-browser.cjs'
if prior.exists():
 t=prior.read_text(encoding='utf-8').replace('console.error(error.stack);','').replace("console.error('FIXTURE_CLEANUP_FAILED:'+error.message);","fs.writeFileSync(path.join(fixture,'cleanup-failure.json'),JSON.stringify({reason:'FIXTURE_CLEANUP_FAILED'}));")
 t=t.replace("console.log(JSON.stringify({status:'PASS',fixture}));",'')
 prior.write_text(t,encoding='utf-8')
print('TIMELINE020_FIXTURE_READY')
