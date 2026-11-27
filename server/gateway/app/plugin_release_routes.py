"""Authenticated administration and delivery of reviewed enterprise bundles."""
import asyncio
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError
from . import enterprise_tasks, plugin_distribution as dist
from . import skill_network_policy as network_policy
from .plugin_packages import MAX_ARCHIVE, parse_package

router=APIRouter(route_class=enterprise_tasks.CloudRoute)
Client=Literal['desktop','web']
def configured_admin(request):
    from .plugin_admin import configured_admin as check
    return check(request)

class Payload(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)

class Upload(Payload):
    filename:str=Field(min_length=1,max_length=180)
    content_base64:str=Field(min_length=4,max_length=4*((MAX_ARCHIVE+2)//3))

class Pin(Payload):
    sha256:str=Field(pattern=r'^[a-f0-9]{64}$')

class ReleaseChange(Pin):
    expected_state: Literal['draft','published','withdrawn'] | None = None
    expected_updated_at: AwareDatetime | None = Field(default=None, strict=False)

class Receipt(Pin):
    client:Client
    plugin_key:str=Field(min_length=1,max_length=64)
    version:str=Field(min_length=1,max_length=32)
    status:Literal['installed','removed','failed']
    device_id:UUID | None=Field(default=None,strict=False)
    usable:bool | None=None


class NetworkReview(Payload):
    skill:str=Field(min_length=1,max_length=160)
    network_mode:Literal['public','confidential']
    expected_revision:int=Field(ge=0)


@router.get('/v1/plugin-admin/skill-network')
async def network_catalog(request:Request):
    configured_admin(request)
    return {'items': await asyncio.to_thread(network_policy.policy_catalog)}


@router.post('/v1/plugin-admin/skill-network')
async def review_network(body:NetworkReview,request:Request):
    admin=configured_admin(request)
    return await asyncio.to_thread(network_policy.review,admin,body.skill,body.network_mode,body.expected_revision)


@router.get('/v1/plugin-admin/releases')
async def releases(request:Request):
    configured_admin(request)
    return {'items':await asyncio.to_thread(dist.releases)}


@router.post('/v1/plugin-admin/releases')
async def upload(request:Request):
    admin=configured_admin(request)
    maximum=4*((MAX_ARCHIVE+2)//3)+1024
    if request.headers.get('content-type','').split(';')[0].strip().lower()!='application/json':
        raise HTTPException(415,'plugin_upload_requires_json')
    length=request.headers.get('content-length')
    if length and (not length.isdigit() or len(length)>12 or int(length)>maximum):
        raise HTTPException(413,'plugin_upload_too_large')
    data=bytearray()
    async for block in request.stream():
        if len(data)+len(block)>maximum:raise HTTPException(413,'plugin_upload_too_large')
        data.extend(block)
    try:body=Upload.model_validate_json(data)
    except ValidationError:raise HTTPException(422,'plugin_invalid_upload') from None
    package=await asyncio.to_thread(parse_package,body.filename,body.content_base64)
    return await asyncio.to_thread(dist.create_release,admin,package)


@router.post('/v1/plugin-admin/releases/{key}/{version}/{action}')
async def update_release(key:str,version:str,action:Literal['preflight','publish','withdraw'],body:ReleaseChange,request:Request):
    return await asyncio.to_thread(dist.change_state,configured_admin(request),key,version,body.sha256,action,
                                   body.expected_state,body.expected_updated_at)


@router.get('/v1/plugin-admin/activity')
async def activity(request:Request,employee:str=''):
    configured_admin(request)
    if employee and not dist.USER.fullmatch(employee):raise HTTPException(422,'invalid_employee')
    return {'items':await asyncio.to_thread(dist.activity,employee)}


async def _employee(request,client):
    actor=await enterprise_tasks._actor(request)
    if client!=('desktop' if request.headers.get('authorization') else 'web'):
        raise HTTPException(403,'plugin_client_identity_mismatch')
    return enterprise_tasks.actor_fields(actor)[0]


@router.get('/v1/plugins/distribution')
async def catalog(request:Request,client:Client):
    employee=await _employee(request,client)
    result=await asyncio.to_thread(dist.distribution_for_employee,employee,client)
    return {**result, 'items':[{k:v for k,v in row.items() if k!='network_mode'} for row in result['items']]}


@router.get('/v1/plugins/distribution/{key}/{version}/skill')
async def skill(key:str,version:str,sha256:str,request:Request,client:Client):
    row=await asyncio.to_thread(dist.skill_for_employee,await _employee(request,client),key,version,sha256,client)
    return {k:v for k,v in row.items() if k!='network_mode'}


@router.get('/v1/plugins/distribution/{key}/{version}/package')
async def package(key:str,version:str,request:Request,client:Client):
    data,sha=await asyncio.to_thread(dist.archive_for_employee,await _employee(request,client),key,version,client)
    return Response(data,media_type='application/zip',headers={'X-Content-Type-Options':'nosniff','ETag':'"'+sha+'"',
            'Content-Disposition':f'attachment; filename="{key}-{version}.zip"'})


@router.post('/v1/plugins/distribution/receipt')
async def receipt(body:Receipt,request:Request):
    employee=await _employee(request,body.client)
    if body.client=='web' and (body.device_id is not None or body.usable is not None):
        raise HTTPException(403,'web_runtime_receipt_required')
    return await asyncio.to_thread(dist.record_receipt,employee,body.client,body.plugin_key,body.version,body.sha256,body.status,
                                   device_id=body.device_id,usable=body.usable)
