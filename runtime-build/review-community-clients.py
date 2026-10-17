from pathlib import Path
import re,json,hashlib
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
exports=set(x.strip().split(' as ')[-1] for x in (base/'dsh-client-ui-primitives/lib/index.js').read_text().split('export {')[-1].split('};')[0].split(','))
all=[]
for pkg in ('file-change-viewer','web-style','widget-kit','token-meter'):
 p=Path('/home/megmeet/dsh020-r41/base-export/@dshp')/pkg
 meta=json.loads((p/'package.json').read_text());s=(p/'lib/client.js').read_text();req=dict(re.findall(r'(?:var|const|let)\s+(\w+)\s*=\s*(?:__toESM\()?require\("([^"]+)"\)',s));acc=set();other={}
 for name,module in req.items():
  props=set(re.findall(r'\b'+re.escape(name)+r'\.(\w+)',s))
  if module=='@deepseek-ai/dsh-client-ui-primitives':acc.update(props)
  else:other[module]=sorted(set(other.get(module,[]))|props)
 missing=sorted(acc-exports);mapping={}
 for old in missing:
  current=re.sub(r'(?:14|16|18|20|24)$','Regular',old)
  mapping[old]=current if current in exports else None
 x={'package':pkg,'version':meta['version'],'peer':meta.get('peerDependencies',{}),'primitives':sorted(acc),'missing':missing,'mapping':mapping,'other_requires':other,'require_sites':re.findall(r'.{0,90}require\([^\n]{0,140}',s),'source_sha256':hashlib.sha256(s.encode()).hexdigest()};all.append(x)
print(json.dumps(all,ensure_ascii=False,indent=2))
Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime/community020-four-client-contracts.json').write_text(json.dumps(all,ensure_ascii=False,indent=2)+'\n')
