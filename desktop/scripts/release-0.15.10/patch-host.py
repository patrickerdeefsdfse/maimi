"""Run the private DSH child under the signed Node version supported by its native loader."""
import os
from prepare import HERE, STAGE, digest, save

rel='resources/app/maimi/company-desktop-session.mjs'
file=STAGE/rel
text=file.read_text('utf8')
old="""  if(process.versions.electron)env.ELECTRON_RUN_AS_NODE='1';
  child=fork(new URL('./company-host-child.mjs',import.meta.url),[],{cwd:workspace,execArgv:['--expose-internals'],env,silent:true});"""
new="""  // The Windows native loader supports standalone Node 24; keep Electron as the UI process.
  const windowsHost=process.platform==='win32' && Boolean(process.versions.electron);
  const executable=windowsHost
   ? fileURLToPath(new URL('../../runtime/primary-runtime/dependencies/node/bin/node.exe',import.meta.url))
   : process.execPath;
  if(!windowsHost && process.versions.electron)env.ELECTRON_RUN_AS_NODE='1';
  child=fork(new URL('./company-host-child.mjs',import.meta.url),[],{cwd:workspace,execPath:executable,execArgv:['--expose-internals'],env,silent:true});"""
if old in text:
    assert text.count(old)==1 and text.count('import {fork}')==1
    text=text.replace('import {fork}',"import {fileURLToPath} from 'node:url';\nimport {fork}").replace(old,new)
    temp=file.with_name(file.name+'.next')
    with temp.open('x',encoding='utf8',newline='') as f:f.write(text)
    os.replace(temp,file)
else:assert new in text
save(HERE/'native-process.json',dict(file=rel,sha256=digest(file),worker='packaged standalone node',parent='Electron',
    compatibility_matrix='https://github.com/deepseek-ai/dsh-node-addon-require-builtin/blob/master/README.md'))
