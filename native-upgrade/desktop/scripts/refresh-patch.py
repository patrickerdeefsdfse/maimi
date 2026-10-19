"""Regenerate the three-file upstream patch without staging or installing upstream."""
from pathlib import Path
import ast,difflib,hashlib,json,sys
root=Path(__file__).resolve().parents[1]
source=root.parent/'upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317'
program=ast.parse((root/'scripts/prepare-native-shell.py').read_text(encoding='utf-8'))
scope={}
functions=[item for item in program.body if isinstance(item,ast.FunctionDef) and item.name in ('one','main','builder')]
exec(compile(ast.Module(body=functions,type_ignores=[]),'candidate-pure-transforms','exec'),scope)
pins={item['path']:item for item in json.loads((root/'upstream-source-lock.json').read_text(encoding='utf-8'))['files']}
transforms={
 'apps/desktop/src/main.ts':scope['main'],
 'apps/desktop/scripts/electron-builder-config.mjs':scope['builder'],
 'apps/desktop-host/src/index.ts':lambda value:scope['one'](value,"import * as desktopOffice from './office.ts'","import * as desktopOffice from './office.ts'\n// Expose the unchanged native Office composition to the company Host adapter.\nexport { desktopOffice }\nexport { installOfficeEngineResolution } from './office-engine.ts'"),
}
patch=[];changes=[];main_source=None
for relative,transform in transforms.items():
 raw=(source/relative).read_bytes()
 if hashlib.sha256(raw).hexdigest()!=pins[relative]['sha256']:raise RuntimeError('UPSTREAM_SOURCE_CHANGED')
 before=raw.decode('utf-8');after=transform(before)
 if relative=='apps/desktop/src/main.ts':main_source=after
 patch.extend(difflib.unified_diff(before.splitlines(True),after.splitlines(True),'a/'+relative,'b/'+relative))
 changes.append({'path':relative,'upstream_sha256':pins[relative]['sha256'],'candidate_sha256':hashlib.sha256(after.encode()).hexdigest()})
if sys.argv[1:]==['--emit-main']:
 sys.stdout.buffer.write(main_source.encode('utf-8'))
elif not sys.argv[1:]:
 (root/'patches/native-shell.patch').write_text(''.join(patch),encoding='utf-8',newline='\n')
 (root/'patch-generation.json').write_text(json.dumps({'schema':'monday-desktop-patch-v1','changed_upstream_files':changes,'copied_upstream_tree':False,'services_changed':False},indent=2)+'\n',encoding='utf-8')
 print(json.dumps({'status':'PATCH_REGENERATED','upstream_files':len(changes),'copied_upstream_tree':False}))
else:raise RuntimeError('ARGUMENT_INVALID')
