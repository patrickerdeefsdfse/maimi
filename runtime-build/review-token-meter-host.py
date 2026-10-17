from pathlib import Path
import re
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for pkg in ('dsh-settings','dsh-session-query','dsh-session-query-sqlite'):
 p=base/pkg/'lib/index.js';print('PACKAGE',pkg,'exists',p.exists())
 if p.exists():
  s=p.read_text()
  for needle in ('update(', 'installSection(', 'listSessions(', 'readSession('):
   for m in list(re.finditer(re.escape(needle),s))[:4]:print('SNIP',needle,s[max(0,m.start()-200):m.start()+1000])
for p in base.glob('*/lib/*.js'):
 if p.stat().st_size>2000000:continue
 s=p.read_text()
 if 'requestRejection(request)' in s or 'requestRejection(req)' in s:
  print('REQUESTREJECTION',p)
  for m in re.finditer(r'requestRejection\((?:request|req)\)',s): print(s[max(0,m.start()-180):m.start()+1300])
old=Path('/home/megmeet/dsh020-r41/base-export/@dshp/token-meter/lib/host.js').read_text()
for needle in ('function createSecretResolver','function registerStatsRoutes','function registerIdentityRoute','sessionQuery.listSessions','sessionQuery.readSession'):
 i=old.find(needle);print('TOKEN',needle,old[max(0,i):i+6000])
