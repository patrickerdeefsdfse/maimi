from pathlib import Path
import re,json
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for pkg in ('token-meter','widget-kit'):
 s=(Path('/home/megmeet/dsh020-r41/base-export/@dshp')/pkg/'lib/client.js').read_text()
 req=dict(re.findall(r'var (\w+) = (?:__toESM\()?require\("([^"]+)"\)',s));acc=set()
 for name,module in req.items():
  if module=='@deepseek-ai/dsh-client-ui-primitives':acc.update(re.findall(r'\b'+name+r'\.(\w+)',s))
 exports=set(x.strip() for x in (base/'dsh-client-ui-primitives/lib/index.js').read_text().split('export {')[-1].split('};')[0].split(','))
 print('CLIENT_EXPORT_CONTRACT',json.dumps({'package':pkg,'requires':sorted(acc),'missing':sorted(acc-exports)}))
for pkg in ('dsh-client-ui-conversation','dsh-client-ui-settings','dsh-client-ui-settings-shell','dsh-client-ui-shell'):
 for p in (base/pkg/'lib').glob('*.js'):
  s=p.read_text();print('SLOTS',pkg,[(k,k in s) for k in ('conversation.view','settings.section','shell.overlay','conversation.session.header.utilities')])
s=(base/'dsh-settings/lib/index.js').read_text();i=s.find('async write(');print('SETTINGS_WRITE',s[i:i+2000])
s=Path('/home/megmeet/dsh020-r41/base-export/@dshp/token-meter/lib/host.js').read_text()
for needle in ('function registerStatsRoutes','function foldSession','function readViaFallback','var NS ='):
 i=s.find(needle);print('TOKEN_HOST',needle,s[i:i+(4000 if needle=='function foldSession' else 2200)])
