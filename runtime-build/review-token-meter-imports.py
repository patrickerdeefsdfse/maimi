from pathlib import Path
import re,json
base=Path('/home/megmeet/dsh020-r41/base-export/@dshp')
for pkg in ('token-meter','widget-kit'):
 for part in ('host','client'):
  p=base/pkg/'lib'/f'{part}.js';s=p.read_text()
  req=dict(re.findall(r'var (\w+) = (?:__toESM\()?require\("([^"]+)"\)',s))
  print(json.dumps({'package':pkg,'part':part,'imports':re.findall(r'^import .*?;$',s,re.M),'requires':req,'accesses':{name:sorted(set(re.findall(r'\b'+name+r'\.(\w+)',s))) for name in req}},ensure_ascii=False))
  for needle in ('function apply(', 'var inject =', 'const inject ='):
   i=s.rfind(needle)
   if i>=0:print(s[i:i+7000])
