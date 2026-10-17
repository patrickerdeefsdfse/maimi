from pathlib import Path
base=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai')
for slot in ('settings.section','shell.overlay','conversation.view','conversation.session.header.utilities'):
 out=[]
 for p in base.glob('*/lib/*.js'):
  if p.stat().st_size<2000000 and slot in p.read_text():out.append(p.parent.parent.name+'/'+p.name)
 print('SLOT',slot,out)
s=Path('/home/megmeet/dsh020-r41/base-export/@dshp/token-meter/lib/host.js').read_text()
for needle in ('function createFolder','function settingsNamespace'):
 i=s.find(needle);print(needle,s[i:i+(6800 if 'Folder' in needle else 600)])
