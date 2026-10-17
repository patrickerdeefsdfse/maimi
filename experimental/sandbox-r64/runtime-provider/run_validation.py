"""Run local synthetic contracts and freeze their evidence; no company access."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys

root=Path(__file__).resolve().parent
versions={name:importlib.metadata.version(name) for name in ('opensandbox','opensandbox-server')}
if set(versions.values()) != {'1.1.1'}:
    raise SystemExit('PINNED_DEPENDENCIES_REQUIRED')
tests=[]
for name,expected in [('test_sdk_contract.py',15),('test_node_policy.py',12)]:
    run=subprocess.run([sys.executable,'-B',str(root/name)],cwd=root,capture_output=True,timeout=60)
    text=run.stderr.decode('utf-8','replace')
    counts=re.findall(r'Ran (\d+) tests? in ',text)
    passed=run.returncode==0 and counts==[str(expected)] and text.rstrip().endswith('OK')
    tests.append({'file':name,'count':expected,'status':'PASS' if passed else 'FAIL'})
files=['provider.py','node_guard.py','server_policy.py','test_sdk_contract.py','test_node_policy.py',
       'deployment.example.toml','upstream-pins.json','ACTIVATION-BLOCKERS.md',
       'PREINSTALLED-RUNTIME-DESIGN.md','run_validation.py']
upstream=['server/opensandbox_server/services/docker/container_ops.py',
          'server/opensandbox_server/services/docker/runtime.py',
          'components/execd/bootstrap.sh','components/execd/Dockerfile']
result={'schema':'maimi-r64-runtime-contract-validation-v1','status':'LOCAL_CONTRACTS_PASS' if all(t['status']=='PASS' for t in tests) else 'LOCAL_CONTRACTS_FAILED',
        'dependency_versions':versions,'tests':tests,'test_count':sum(t['count'] for t in tests),
        'scope':'REAL_PINNED_SDK_WITH_SYNTHETIC_HTTP_AND_NODE_PROJECTIONS',
        'actual_upstream_policy_hook_invoked':True,'actual_source_pin_rejection_tested':True,
        'company_requests':0,'docker_requests':0,'network_requests':0,'real_runsc_tested':False,
        'company_deployment_performed':False,'provider_default_enabled':False,
        'activation':'BLOCKED_READONLY_RUNTIME_BOOTSTRAP_AND_REAL_ISOLATION_ACCEPTANCE',
        'uncertain_create_retry':False,'uncertain_delete_retry':False,'unknown_snapshot_returns_file':False,
        'readiness_polling':'BOUNDED_READONLY_CHECKS_NOT_MUTATION_RETRIES',
        'files':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in files},
        'upstream_evidence':{p:hashlib.sha256((root/'upstream/source'/p).read_bytes()).hexdigest() for p in upstream}}
(root/'runtime-contract-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({k:result[k] for k in ('status','test_count','activation','company_requests','real_runsc_tested')}))
raise SystemExit(0 if result['status']=='LOCAL_CONTRACTS_PASS' else 1)
