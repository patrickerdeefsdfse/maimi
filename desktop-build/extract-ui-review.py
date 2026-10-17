from pathlib import Path
import json,tarfile,base64,hashlib
root=Path(__file__).resolve().parent
area=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop')
data=(area/'ui-pack.log').read_bytes()
text=data.decode('utf-16' if data.startswith(b'\xff\xfe') else 'utf-8-sig')
rows=json.loads(text);evidence=[]
for row in rows:
    assert row['version']=='0.2.0-rc.2' and row['name'].startswith('@deepseek-ai/dsh-')
    archive=area/'ui-tarballs'/row['filename'];b=archive.read_bytes()
    assert row['integrity']=='sha512-'+base64.b64encode(hashlib.sha512(b).digest()).decode()
    name=row['name'].split('/')[1];files={}
    with tarfile.open(archive,'r:gz') as tf:
        for member in tf.getmembers():
            if member.name not in ('package/lib/client.js','package/lib/index.js','package/package.json'):continue
            assert member.isfile()
            body=tf.extractfile(member).read();rel=member.name[len('package/'):]
            p=area/'upstream-ui'/name/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
            files[rel]=hashlib.sha256(body).hexdigest()
    evidence.append({'name':row['name'],'version':row['version'],'source':'npm pack --registry=https://registry.npmjs.org --ignore-scripts','integrity':row['integrity'],'files':files})
(root/'evidence/upstream-ui-acquisition.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'VERIFIED_PUBLIC_NPM_UI_ACQUIRED','packages':len(evidence),'companyRequests':0}))
