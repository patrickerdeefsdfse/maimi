from pathlib import Path
import json

root=Path(__file__).resolve().parent
large=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')
app=root/'app'
p=app/'package.json'; pkg=json.loads(p.read_text(encoding='utf-8-sig'));pkg['version']='0.13.0';p.write_text(json.dumps(pkg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
p=app/'scripts/package.cjs';s=p.read_text(encoding='utf-8')
s=s.replace("out: path.join(root, 'dist', version + (company ? '-company' : ''))", "out: path.join("+json.dumps(str(large/'packages'))+", version + (company ? '-company' : ''))")
p.write_text(s,encoding='utf-8')
p=root/'build-candidate-installer.cjs';s=p.read_text(encoding='utf-8')
s=s.replace("const prepackaged=path.join(app,'dist',version+'-company','MEGMEET AI-win32-x64');", "const prepackaged=path.join("+json.dumps(str(large/'packages'))+",version+'-company','MEGMEET AI-win32-x64');")
s=s.replace("evidence.runtime.dsh!=='0.1.7-rc.2'", "evidence.runtime.dsh!=='0.2.0-rc.2'")
s=s.replace("const output=path.join(root,'build-only','installer-'+version)", "const output=path.join("+json.dumps(str(large/'build-only'))+",'installer-'+version)")
p.write_text(s,encoding='utf-8')
(root/'build-locations.json').write_text(json.dumps({'runtime':str(large/'runtime-win32-x64'),'packages':str(large/'packages'),'buildOnly':str(large/'build-only'),'publicRegistry':'https://registry.npmjs.org','dshVersion':'0.2.0-rc.2','desktopVersion':'0.13.0','companyRequests':0},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('BUILD_LOCATIONS_AND_VERSION_PREPARED')
