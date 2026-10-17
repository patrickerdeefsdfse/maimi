from pathlib import Path
import re
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for pkg in ('dsh-agent-default-model','dsh-client-connection','dsh-settings','dsh-config-editor'):
 p=base/pkg/'lib/index.js'
 if not p.exists():continue
 s=p.read_text();print('PKG',pkg)
 if pkg=='dsh-agent-default-model':print(s[:7000])
 elif pkg=='dsh-client-connection':
  for needle in ('constructor(', 'browserAuth =', 'createBrowserAuth', 'token', 'dispose()'):
   for m in list(re.finditer(re.escape(needle),s))[:3]:print('SNIP',needle,s[max(0,m.start()-120):m.start()+1700])
 elif pkg=='dsh-config-editor':
  for needle in ('async edit(', 'volatile-update', 'reload', 'replace'):
   for m in list(re.finditer(re.escape(needle),s))[:4]:print('SNIP',needle,s[max(0,m.start()-150):m.start()+1100])
