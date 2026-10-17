from pathlib import Path
import hashlib,json,sys
R=Path('/mnt/d/麦格米特/linux/output/dsh020-rc2-r41-20261008/runtime')
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
archive=Path('/mnt/c/Users/LI YUHAO/.codex/tmp/dsh020-r41-linux-delivery/runtime-linux-x64-020-rc2.tar.gz')
pins=json.loads((R/'source-pins.json').read_text())
report={'schema':'megmeet-dsh020-runtime-delivery-v1','runtime':'0.2.0-rc.2','node':'22.20.0','archive':{'filename':archive.name,'path':r'C:\Users\LI YUHAO\.codex\tmp\dsh020-r41-linux-delivery\runtime-linux-x64-020-rc2.tar.gz','sha256':sha(archive),'size_bytes':archive.stat().st_size},'Dockerfile':{'filename':'Dockerfile','sha256':sha(R/'Dockerfile')},'base':{'configured_ref':'sha256:39012d4ef9b49c3694486c6b7b5c87c8b0cc9f31d355d1f31a06a5ca6a3a8101','linux_amd64_id':'sha256:dac622ca2c741fd56878b811876def81c901c9685b5afd8122049e89fbe8b33a'},'local_test_image':sys.argv[1].strip(),'local_test_base_differs_from_company':True,'source_targets':pins['source_targets'],'dsh_package_count':sum(n.startswith('@deepseek-ai/dsh') for n in pins['packages']),'deepseek_packages':pins['packages'],'company_requests':0,'company_runtime_acceptance':'PENDING'}
(R/'runtime-delivery.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'status':'DELIVERY_EXPORTED','archive_bytes':archive.stat().st_size,'archive_sha256':report['archive']['sha256']}))
