from pathlib import Path
import hashlib,json
p=Path('/home/megmeet/dsh020-r41/runtime-linux-x64/node_modules/@deepseek-ai/dsh-web-frontend/dist/assets')
out=Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime/frontend-assets.json')
files=sorted(x for x in p.iterdir() if x.is_file() and x.suffix in {'.js','.css'})
assert len(files)==4
rows=[]
for f in files:
 b=f.read_bytes();rows.append({'route':'/assets/'+f.name,'size_bytes':len(b),'sha256':hashlib.sha256(b).hexdigest(),'mime':'text/javascript' if f.suffix=='.js' else 'text/css','archive_path':'opt/dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/assets/'+f.name})
out.write_text(json.dumps({'runtime':'0.2.0-rc.2','assets':rows},indent=2)+'\n')
print(json.dumps({'status':'EXPORTED','assets':len(rows)}))
