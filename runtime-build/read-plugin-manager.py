from pathlib import Path
r=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for d in r.iterdir():
 if 'plugin-manager' in d.name:
  p=d/'lib/index.js';ls=p.read_text().splitlines()
  for i,l in enumerate(ls):
   if any(x in l for x in ['setPluginEnabled(', 'setBundleEnabled(', 'removeBundle(', 'super(ctx','super(' , 'Service','PluginManager']):print(d.name,i+1,'\n'.join(ls[max(0,i-1):i+5]))
