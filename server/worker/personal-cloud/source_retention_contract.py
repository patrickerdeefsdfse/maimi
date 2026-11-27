"""Closed wire contract for exact offline workspace-source reclamation."""
import re
from uuid import UUID

PROTOCOL='source-retention-v1'
MAX_REQUEST_BYTES=160*1024
MAX_RESULT_BYTES=32768
MAX_FILE_BYTES=128*1024*1024
MAX_TOTAL_BYTES=256*1024*1024
REASONS=frozenset(('VERSION_CHANGED','CONTENT_CHANGED','UNSAFE_PATH','NOT_REGULAR','PROTECTED_PATH','IO_UNCONFIRMED'))
SHA=re.compile(r'^[a-f0-9]{64}$')


def need(ok):
    if not ok:raise ValueError('SOURCE_RETENTION_CONTRACT_INVALID')


def uuid(value):
    try:need(isinstance(value,str) and str(UUID(value))==value)
    except (ValueError,TypeError,AttributeError):raise ValueError('SOURCE_RETENTION_CONTRACT_INVALID') from None
    return value


def validate_request(value):
    need(type(value) is dict and set(value)=={'protocol','operation_id','refs'} and value['protocol']==PROTOCOL)
    uuid(value['operation_id']);refs=value['refs'];need(type(refs) is list and 1<=len(refs)<=32)
    seen=set();total=0;clean=[]
    for row in refs:
        need(type(row) is dict and set(row)=={'source_id','path','version_hash','sha256','bytes','runtime_fingerprint'})
        sid=uuid(row['source_id']);need(sid not in seen);seen.add(sid)
        path=row['path']
        need(isinstance(path,str) and path.startswith('/workspace/') and len(path)<=4096 and
             not re.search(r'[\\\x00-\x1f\x7f]',path) and all(p not in ('','.','..') for p in path.split('/')[1:]))
        need(all(isinstance(row[key],str) and SHA.fullmatch(row[key]) for key in ('sha256','version_hash','runtime_fingerprint')))
        need(type(row['bytes']) is int and 0<=row['bytes']<=MAX_FILE_BYTES)
        total+=row['bytes'];need(total<=MAX_TOTAL_BYTES);clean.append(dict(row))
    return {'protocol':PROTOCOL,'operation_id':value['operation_id'],'refs':clean}


def validate_result(value,request):
    request=validate_request(request);expected={row['source_id']:row for row in request['refs']}
    need(type(value) is dict and set(value)=={'status','operation_id','items'} and value['operation_id']==request['operation_id'])
    need(value['status'] in ('COMPLETE','PARTIAL') and type(value['items']) is list and len(value['items'])==len(expected))
    seen=set();retained=False
    for row in value['items']:
        need(type(row) is dict and row.get('source_id') in expected and row['source_id'] not in seen)
        seen.add(row['source_id']);need(type(row.get('reclaimed_bytes')) is int)
        if row.get('status')=='RETAINED':
            retained=True;need(set(row)=={'source_id','status','reclaimed_bytes','reason'} and row['reclaimed_bytes']==0 and row['reason'] in REASONS)
        else:
            need(set(row)=={'source_id','status','reclaimed_bytes'} and row.get('status') in ('DELETED','ABSENT'))
            need(row['reclaimed_bytes']==(expected[row['source_id']]['bytes'] if row['status']=='DELETED' else 0))
    need((value['status']=='PARTIAL')==retained)
    return value
