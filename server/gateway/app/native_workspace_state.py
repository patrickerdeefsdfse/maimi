"""Exact worker-capability route. No browser owner selectors or automatic migration."""
import asyncio,hashlib,json,os,re
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse
from .history_store import _connect
from .iam_oauth import IAMFailure
from .workspace_state import WorkspaceStateStore,WorkspaceConflict
router=APIRouter();PREFIX='/internal/native-workspace-state';OPS={'load','write'}
HEADERS={'Cache-Control':'no-store','Pragma':'no-cache'}
def need(ok,code,status=400):
    if not ok:raise IAMFailure(status,code)
def internal_route(request):return request.method=='POST' and request.url.path in {PREFIX+'/'+x for x in OPS}
def schema_ready():
    with _connect() as conn,conn.cursor() as cur:
        cur.execute('SELECT owner_key,unit_name,format_version,revision,value,writer_generation,last_write_id,content_sha256,updated_at FROM platform.native_workspace_state LIMIT 0')
        cur.execute("SELECT has_table_privilege(current_user,'platform.native_workspace_state','SELECT') AND has_table_privilege(current_user,'platform.native_workspace_state','INSERT') AND has_table_privilege(current_user,'platform.native_workspace_state','UPDATE') AS allowed")
        need(cur.fetchone()['allowed'] is True,'workspace_database_privileges_missing',503)
def validate_document(value):
    need(type(value) is dict and set(value)=={'tables','global'},'workspace_document_invalid')
    need(type(value['tables']) is dict and set(value['tables'])=={'workspaces'} and type(value['tables']['workspaces']) is dict,'workspace_document_invalid')
    need(value['global'] is None or type(value['global']) is dict,'workspace_document_invalid')
    need(len(value['tables']['workspaces'])<=1024,'workspace_document_limit',413)
    for key,record in value['tables']['workspaces'].items():
        need(isinstance(key,str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,199}',key) and key not in ('constructor','prototype','__proto__') and type(record) is dict,'workspace_record_invalid')
    # Pending create/delete intentionally permits temporary list/table divergence.

@router.post(PREFIX+'/{operation}')
async def internal(request:Request,operation:str):
    from .personal_cloud import manager
    need(os.getenv('MAIMI_WORKSPACE_STATE_BACKEND')=='postgres','workspace_backend_not_enabled',503)
    need(operation in OPS,'workspace_operation_invalid',404)
    need(not request.headers.get('origin') and not request.headers.get('sec-fetch-site'),'workspace_browser_request_refused',403)
    owner=request.headers.get('x-maimi-owner','');token=request.headers.get('authorization','');generation=request.headers.get('x-maimi-generation','')
    need(re.fullmatch('[a-f0-9]{32}',owner),'workspace_owner_invalid')
    need(re.fullmatch('Bearer [A-Za-z0-9_-]{43}',token),'workspace_authentication_required',401)
    need(re.fullmatch('[1-9][0-9]{0,15}',generation),'workspace_generation_invalid')
    generation=int(generation)
    # Same accepted manager route-token check as native history; transaction
    # fencing below independently closes its authorization-to-commit race.
    authorized=await manager('POST','/v1/hosts/'+owner+'/history-authorize',{'generation':generation,'route_token':token[7:]})
    need(authorized=={'authorized':True,'generation':generation},'workspace_authentication_required',403)
    need(request.headers.get('content-type','').split(';')[0].lower()=='application/json','workspace_content_type_invalid',415)
    raw=bytearray()
    async for chunk in request.stream():
        need(len(raw)+len(chunk)<=4*1024**2+4096,'workspace_request_too_large',413);raw.extend(chunk)
    try:body=json.loads(raw,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except (ValueError,UnicodeError,RecursionError):raise IAMFailure(400,'workspace_request_invalid') from None
    need(type(body) is dict,'workspace_request_invalid')
    store=WorkspaceStateStore(_connect)
    try:
        if operation=='load':
            need(body=={},'workspace_request_invalid')
            value=await asyncio.to_thread(store.load,owner,generation)
        else:
            need(set(body)=={'expected_revision','write_id','value'},'workspace_request_invalid')
            rev=body['expected_revision'];need(isinstance(rev,str) and re.fullmatch('0|[1-9][0-9]{0,18}',rev) and int(rev)<2**63-1,'workspace_revision_invalid')
            validate_document(body['value'])
            value=await asyncio.to_thread(store.compare_and_swap,owner,generation,int(rev),body['write_id'],body['value'])
            value['write_id']=body['write_id']
        value.update(unit='workspace',format_version=2)
        return JSONResponse(value,headers=HEADERS)
    except WorkspaceConflict as error:
        code=str(error);status=403 if code=='WORKSPACE_GENERATION_NOT_ADMITTED' else 413 if code=='WORKSPACE_DOCUMENT_TOO_LARGE' else 409
        raise IAMFailure(status,'workspace_'+code.lower()) from None
    except IAMFailure:raise
    except Exception:
        # A timeout can follow commit; return no raw DB error/data and let the
        # worker poison this generation rather than replaying the write.
        raise IAMFailure(503,'workspace_database_outcome_unconfirmed') from None
