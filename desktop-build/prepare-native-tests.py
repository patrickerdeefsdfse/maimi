from pathlib import Path
import json,shutil
root=Path(__file__).resolve().parent
repo=root.parents[2]
large=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')
out=root/'native-tests';(out/'test-app').mkdir(parents=True,exist_ok=True)
for p in (root/'app').iterdir():
 if p.is_file() and p.suffix in ('.cjs','.mjs','.json','.html','.js') and not p.name.startswith('artifact'):
  shutil.copyfile(p,out/'test-app'/p.name)
dest=out/'test-app/deployment.cjs';dest.write_text(dest.read_text(encoding='utf-8').replace('28090','28690'),encoding='utf-8')
(out/'test-app/deployment.json').write_text(json.dumps({'mode':'local-development','gatewayOrigin':'http://127.0.0.1:28690'})+'\n',encoding='utf-8')
for name in ('test-actual-host.cjs','test-dws-native.cjs'):
 s=(repo/'output/dsh-rc2-implementation-20260928/ui'/name).read_text(encoding='utf-8')
 s=s.replace("const runtimeRoot=path.resolve(__dirname,'runtime-win32-x64');",'const runtimeRoot='+json.dumps(str(large/'runtime-win32-x64'))+';')
 s=s.replace("const scope=path.join(__dirname,'synthetic-fixtures');",'const scope='+json.dumps(str(large/'synthetic-fixtures'))+';')
 (out/name).write_text(s,encoding='utf-8')
print('NEW_NATIVE_REAL_HOST_HARNESS_PREPARED')
