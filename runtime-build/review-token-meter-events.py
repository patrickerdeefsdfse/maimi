from pathlib import Path
import re
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for pkg in ('dsh-session','dsh-session-query','dsh-agent-loop','dsh-agent'):
 for p in (base/pkg/'lib').glob('*.js'):
  s=p.read_text()
  for needle in ('"assistant/message"', '"assistant/chunk"'):
   hits=list(re.finditer(re.escape(needle),s))
   if hits:print('EVENT',pkg,needle,s[max(0,hits[0].start()-250):hits[0].start()+1000])
