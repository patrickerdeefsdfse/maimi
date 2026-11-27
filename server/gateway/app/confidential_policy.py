"""Outbound decisions for the confidential cloud workspace, made at the gateway."""
import asyncio
import re
from .iam_oauth import IAMFailure


def forbid_external_tools(actor):
    if actor.network_mode != 'public' or getattr(actor,'task_network_mode','public') != 'public':
        raise IAMFailure(503, 'skill_runtime_unavailable')


def check_model_destination(actor, base_url):
    restricted = getattr(actor,'task_network_mode','public') == 'restricted'
    if actor.network_mode != 'public' or (restricted and base_url.rstrip('/') != 'https://model.example.invalid/v1'):
        raise IAMFailure(503,'skill_runtime_unavailable')


SKILL_TAG = re.compile(r'(?<!\S)/([\w-]{1,180})(?!\S)')


def prompt_skill_names(request):
    """DSH's skill menu inserts '/name ' in text parts of the pending turn."""
    names = set()
    if not isinstance(request,dict) or not isinstance(request.get('content'),list):
        raise ValueError('invalid prompt content')
    for part in request.get('content', []):
        if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
            names.update(SKILL_TAG.findall(part['text']))
            if len(names) > 128:
                raise IAMFailure(400, 'too_many_skill_references')
    return names


async def check_model_turn(actor, messages):
    """Check the selected skill even if a client used a different RPC transport."""
    if getattr(actor,'task_network_mode','public') == 'restricted':
        return
    names = set()
    for message in reversed(messages):
        if message.get('role')!='user':continue
        content = message.get('content')
        parts = [{'type':'text', 'text':content}] if isinstance(content,str) else content
        if isinstance(parts,list):
            names.update(prompt_skill_names({'content':parts}))
        break
    if names:
        from .skill_network_policy import resolve_skill_network_mode
        if await asyncio.to_thread(resolve_skill_network_mode, names) == 'confidential':
            raise IAMFailure(503, 'skill_runtime_unavailable')

async def prepare_selected_skills(actor,body,header,owner=None):
    """Only the trusted task relay can supply a lease and its task-scoped selection."""
    if len(header or '')>12000:raise IAMFailure(400,'personal_model_request_invalid')
    names=set(header.split(',')) if header else set()
    if len(names)>128 or any(not re.fullmatch('[A-Za-z0-9_-]{1,180}',n) for n in names):
        raise IAMFailure(400,'personal_model_request_invalid')
    from .skill_network_policy import reviewed_confidential_names,task_instructions
    if getattr(actor,'task_network_mode','public')!='restricted':
        if names and await asyncio.to_thread(reviewed_confidential_names,names):
            raise IAMFailure(503,'skill_runtime_unavailable')
        return body
    if header is None:raise IAMFailure(503,'skill_runtime_unavailable')
    selected=await asyncio.to_thread(task_instructions,actor.user_no,names,owner)
    if not selected:return body
    # Keep company skill text at user level, below system and platform tool policies.
    contents='\n\n'.join('技能 '+name+'\n'+text for name,text in selected)
    messages=list(body['messages'])
    last_user=next((i for i in reversed(range(len(messages))) if messages[i]['role']=='user'),len(messages))
    messages.insert(last_user,{'role':'user','content':'以下是我在当前任务选择的已加载技能说明，请据此处理本轮请求。\n\n'+contents})
    return {**body,'messages':messages}


class ConfidentialEntry:
    """The remote-chat API cannot execute an offline task, even with a valid login."""
    prefix = '/maimi/confidential-api'

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path','')
        if path != self.prefix and not path.startswith(self.prefix + '/'):
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        if scope['type'] == 'websocket':
            return await send({'type':'websocket.close','code':1008})
        response = JSONResponse({'detail':'skill_runtime_unavailable'}, status_code=503,
                                headers={'Cache-Control':'no-store'})
        return await response(scope,receive,send)
