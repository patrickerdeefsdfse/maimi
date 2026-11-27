"""Serve declarative skills to the IAM-bound employee workspace."""
import asyncio
import json
import secrets
from uuid import NAMESPACE_URL, uuid5

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .iam_oauth import IAMFailure


PREFIX = '/internal/personal-cloud/plugin-delivery/'
PUBLICATION = ('plugin_key', 'version', 'sha256')
MENU = (*PUBLICATION, 'name', 'description', 'network_mode')
PATHS = frozenset(PREFIX + operation for operation in ('list', 'get', 'receipt', 'bundled'))


async def handle(request: Request, operation: str):
    from . import plugin_distribution as registry
    from .personal_cloud import HEADERS, lease_actor, owner_key

    if request.url.path not in PATHS:
        raise IAMFailure(404, 'not_found')
    authorization = request.headers.get('authorization', '')
    actor = await lease_actor(authorization[7:] if authorization.startswith('Bearer ') else '')
    owner = request.headers.get('x-maimi-owner', '')
    if not owner.isascii() or not secrets.compare_digest(owner, owner_key(actor)):
        raise IAMFailure(403, 'plugin_workspace_mismatch')
    if getattr(actor,'network_mode','public') == 'confidential':
        # An offline task must be completely provisioned before it starts.
        raise IAMFailure(403, 'confidential_task_offline')
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 2048:
            raise IAMFailure(413, 'plugin_request_too_large')
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise IAMFailure(400, 'plugin_request_invalid') from None
    fields = {'list': (), 'get': PUBLICATION, 'receipt': (*PUBLICATION, 'status'), 'bundled': ()}[operation]
    if not isinstance(body, dict) or set(body) != set(fields) or any(not isinstance(v, str) for v in body.values()):
        raise IAMFailure(400, 'plugin_request_invalid')
    employee = actor.user_no
    mode = getattr(actor, 'network_mode', 'public')
    if operation == 'bundled':
        from .skill_network_policy import bundled_skills_for_workspace
        value = {'skills': await asyncio.to_thread(bundled_skills_for_workspace, employee, mode)}
    elif operation == 'list':
        def catalog():
            snapshot = registry.distribution_for_employee(employee, 'web', network_mode=mode)
            rows, removed = snapshot['items'], snapshot['remove']
            return {'items': [{k: row[k] for k in MENU} for row in rows],
                    'remove': [{k: row[k] for k in PUBLICATION} for row in removed]}
        value = await asyncio.to_thread(catalog)
    else:
        key, version, digest = (body[k] for k in PUBLICATION)
        if operation == 'get':
            row = await asyncio.to_thread(registry.skill_for_employee, employee, key, version, digest, 'web', network_mode=mode)
            value = {k: row[k] for k in (*MENU, 'instructions')}
        elif body['status'] == 'usable':
            # This internal route verifies the worker lease and workspace owner above.
            # A stable opaque workspace ID scopes the positive observation on the web channel.
            workspace_id = uuid5(NAMESPACE_URL, 'maimi.web.plugin-availability/'+owner)
            value = await asyncio.to_thread(registry.record_receipt, employee, 'web', key, version, digest, 'installed',
                                            network_mode=mode, device_id=workspace_id, usable=True)
        else:
            value = await asyncio.to_thread(registry.record_receipt, employee, 'web', key, version, digest, body['status'], network_mode=mode)
    return JSONResponse(jsonable_encoder(value), headers=HEADERS)
