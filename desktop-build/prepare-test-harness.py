from pathlib import Path
import json
root=Path(__file__).resolve().parent
repo=root.parents[2]
large=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')
for name in ('preview-ui.cjs','rehearse-installer.cjs'):
 p=root/'tests'/name;s=p.read_text(encoding='utf-8');s=s.replace('0.12.2','0.13.1').replace('0.12.1','0.13.0');p.write_text(s,encoding='utf-8')
p=root/'tests/verify-candidate.cjs';s=p.read_text(encoding='utf-8')
s=s.replace("const packaged=path.join(app,'dist',version+'-company','MEGMEET AI-win32-x64'),old=", "const packaged=path.join("+json.dumps(str(large/'packages'))+",version+'-company','MEGMEET AI-win32-x64'),old=")
s=s.replace("path.join(root,'evidence','payload-audit-')", "path.join("+json.dumps(str(large))+",'payload-audit-')")
s=s.replace("for(const name of targets.filter(p=>p.startsWith('resources/runtime/')))assert.equal(sha(fs.readFileSync(path.join(packaged,name))),sha(fs.readFileSync(path.join(old,name))));", "const checked=require('../app/dsh-adapter.cjs').validateRuntime(path.join(packaged,'resources/runtime'),{full:true});assert.equal(checked.manifest.dsh,'0.2.0-rc.2');")
s=s.replace("DSHRuntimeManifestsUnchanged:true,dsh:'0.1.7-rc.2'", "DSHRuntimeManifestsValidated:true,dsh:'0.2.0-rc.2'")
p.write_text(s,encoding='utf-8')
print('TEST_HARNESS_0130_PREPARED')
