from pathlib import Path
import urllib.request,json,tarfile,io,base64,hashlib,concurrent.futures
root=Path(__file__).resolve().parent
out=Path(r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-desktop\upstream-ui')
version='0.2.0-rc.2'
names=['dsh-client-ui-brand-official','dsh-client-ui-conversation','dsh-client-ui-workspace','dsh-client-ui-settings-models','dsh-client-ui-chat','dsh-office-to-pdf']
def acquire(name):
    url='https://registry.npmjs.org/@deepseek-ai%2f'+name+'/'+version
    data=json.load(urllib.request.urlopen(url,timeout=30))
    assert data['name']=='@deepseek-ai/'+name and data['version']==version
    archive=urllib.request.urlopen(data['dist']['tarball'],timeout=60).read()
    assert data['dist']['integrity']=='sha512-'+base64.b64encode(hashlib.sha512(archive).digest()).decode()
    pins={}
    with tarfile.open(fileobj=io.BytesIO(archive),mode='r:gz') as tf:
        for member in tf.getmembers():
            if member.name not in ('package/lib/client.js','package/lib/index.js','package/package.json'):continue
            assert member.isfile()
            body=tf.extractfile(member).read();rel=member.name[len('package/'):]
            p=out/name/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(body)
            pins[rel]=hashlib.sha256(body).hexdigest()
    return {'name':data['name'],'version':version,'metadataURL':url,'tarball':data['dist']['tarball'],'integrity':data['dist']['integrity'],'files':pins}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool: evidence=list(pool.map(acquire,names))
(root/'evidence/upstream-ui-acquisition.json').write_text(json.dumps(evidence,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':'VERIFIED_PUBLIC_NPM_UI_ACQUIRED','packages':len(evidence),'companyRequests':0}))
