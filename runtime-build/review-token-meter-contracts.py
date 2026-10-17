from pathlib import Path
import json,re
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
p=base/'dsh-client-ui-primitives/lib/index.js';s=p.read_text();print('PRIMITIVES_END',s[-15000:])
for q in base.iterdir():
 if any(x in q.name for x in ('connection','settings','session-query','web-server','compat')): print('PACKAGE',q.name)
old=Path('/home/megmeet/dsh020-r41/base-export/@dshp/token-meter/lib/client.js').read_text();i=old.rfind('function apply(');print('TOKEN_APPLY',old[i:i+12500])
