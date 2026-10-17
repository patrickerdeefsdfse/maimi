"""Freeze the reviewed four modules into one self-contained server entry."""
import base64
import hashlib
import json
from pathlib import Path
import zlib

HERE=Path(__file__).resolve().parent
NAMES=('private_store','preflight','provider_check','controller')
TEMPLATE='''#!/usr/bin/env python3
"""Fixed r52 Kuaidi100 account rotation; never accepts a key on the command line."""
import sys
if __name__ == '__main__' and not sys.flags.isolated:
 import os
 os.execv(sys.executable,[sys.executable,'-I','-B',os.path.abspath(__file__),*sys.argv[1:]])
import base64,hashlib,json,types,zlib
PAYLOAD_SHA='__PAYLOAD_SHA__'
PACKED='__PACKED__'
def load_bundle():
 raw=zlib.decompress(base64.b64decode(PACKED,validate=True))
 if len(raw)>262144 or hashlib.sha256(raw).hexdigest()!=PAYLOAD_SHA:raise ValueError('BUNDLE_INVALID')
 value=json.loads(raw)
 names=('private_store','preflight','provider_check','controller')
 if value.get('schema')!='kuaidi100-rotation-r53-bundle-v1' or set(value['modules'])!=set(names):raise ValueError('BUNDLE_INVALID')
 loaded={}
 for name in names:
  source=base64.b64decode(value['modules'][name]['source'],validate=True)
  if hashlib.sha256(source).hexdigest()!=value['modules'][name]['sha256']:raise ValueError('BUNDLE_INVALID')
  module=types.ModuleType(name);module.__file__='<r53-bundled-'+name+'>';sys.modules[name]=module
  exec(compile(source,module.__file__,'exec'),module.__dict__);loaded[name]=module
 return loaded
def entry_main():
 try:
  return load_bundle()['controller'].main()
 except BaseException:
  print(json.dumps({'schema':'kuaidi100-key-rotation-r53-v1','status':'STOP','reason':'ENTRY_LOAD_FAILED','credential_values_or_hash_output':False}),flush=True)
  return 1
if __name__=='__main__':raise SystemExit(entry_main())
'''

def build():
    files={name:(HERE/(name+'.py')).read_bytes() for name in NAMES}
    payload=json.dumps({'schema':'kuaidi100-rotation-r53-bundle-v1','modules':{
        name:{'sha256':hashlib.sha256(raw).hexdigest(),'source':base64.b64encode(raw).decode()}
        for name,raw in files.items()}},sort_keys=True,separators=(',',':')).encode()
    entry=TEMPLATE.replace('__PAYLOAD_SHA__',hashlib.sha256(payload).hexdigest()).replace(
        '__PACKED__',base64.b64encode(zlib.compress(payload,9)).decode()).encode()
    path=HERE/'rotate-kuaidi100-key-r53.py';path.write_bytes(entry)
    record={'schema':'kuaidi100-rotation-r53-frozen-entry-v1','entry':path.name,
            'entry_sha256':hashlib.sha256(entry).hexdigest(),'size_bytes':len(entry),
            'payload_sha256':hashlib.sha256(payload).hexdigest(),
            'source_sha256':{name+'.py':hashlib.sha256(raw).hexdigest() for name,raw in files.items()},
            'server_base':'APPLIED_R52_FIXED_IDENTITIES','production_commands':[
                '--stage-key','--check','--apply-maintenance-window','--restore-previous-key'],
            'initial_delivery_command':'--stage-key','stage_restarts':0,
            'maximum_mcp_control_requests_stage':5,'maximum_mcp_control_requests_apply':10,
            'mes_or_tracking_or_eta_queries':0,'local_entry_tests':'PENDING'}
    (HERE/'frozen-entry.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':'FROZEN','size_bytes':len(entry),'entry_sha256':record['entry_sha256']}))
if __name__=='__main__':build()
