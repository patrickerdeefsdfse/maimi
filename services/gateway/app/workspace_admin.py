"""Local server-administrator CLI. No public bootstrap or self-grant HTTP API.

Run inside the configured Gateway environment; its DB credential is read from
the existing secret-backed configuration, never from command arguments.
"""
from __future__ import annotations

import argparse
import json
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from . import enterprise_tasks as cloud


def _text(value, maximum=100):
    if not value or value != value.strip() or len(value)>maximum or any(ord(c)<32 for c in value):
        raise ValueError('invalid administrator argument')
    return value


def execute(args):
    admin=_text(args.operator)
    workspace_id=str(args.workspace) if args.workspace else str(uuid4())
    with cloud._connect() as conn,conn.cursor() as cur:
        if args.command=='create':
            name=_text(args.name,200)
            if args.kind=='department' and not args.organization:
                raise ValueError('department requires a current IAM organization code')
            org=_text(args.organization) if args.organization else 'GROUP:'+workspace_id
            cur.execute('''INSERT INTO platform.hybrid_workspaces(id,kind,organization_code,name,default_model)
                VALUES(%s,%s,%s,%s,%s)''',(workspace_id,args.kind,org,name,_text(args.model,200)))
            action='admin:workspace-created'
        else:
            if not args.workspace:
                raise ValueError('workspace is required')
            cur.execute('SELECT id FROM platform.hybrid_workspaces WHERE id=%s',(workspace_id,))
            if not cur.fetchone():
                raise ValueError('workspace not found')
            if args.command=='member':
                user=_text(args.user)
                cur.execute('''INSERT INTO platform.hybrid_workspace_grants
                    (workspace_id,user_no,enabled,can_chat,can_share,can_publish,can_retrieve)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(workspace_id,user_no) DO UPDATE SET enabled=EXCLUDED.enabled,
                    can_chat=EXCLUDED.can_chat,can_share=EXCLUDED.can_share,can_publish=EXCLUDED.can_publish,
                    can_retrieve=EXCLUDED.can_retrieve''',
                    (workspace_id,user,args.enabled,args.can_chat,args.can_share,args.can_publish,args.can_retrieve))
                action='admin:member-updated'
            elif args.command=='plugin-install':
                manifest=next((m for m in cloud.RUNNERS.values() if m['id']==args.plugin),None)
                if not manifest:
                    raise ValueError('runner is not in the release registry')
                runner=next(k for k,m in cloud.RUNNERS.items() if m['id']==args.plugin)
                policy={'mode':'deterministic-no-model' if manifest['simulation'] else 'actor-model'}
                if args.model and not manifest['simulation']:
                    policy['model']=_text(args.model,200)
                cur.execute('''SELECT runner,model_policy FROM platform.hybrid_plugins
                    WHERE workspace_id=%s AND plugin_key=%s AND version=%s''',(workspace_id,args.plugin,manifest['version']))
                old=cur.fetchone()
                if old and (old['runner']!=runner or old['model_policy']!=policy):
                    raise ValueError('plugin contract already exists; never mutate its fixed version')
                cur.execute('''INSERT INTO platform.hybrid_plugins(workspace_id,plugin_key,version,name,runner,model_policy)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                    (workspace_id,args.plugin,manifest['version'],args.plugin,runner,Jsonb(policy)))
                action='admin:plugin-installed'
            elif args.command=='plugin-member':
                cur.execute('''INSERT INTO platform.hybrid_plugin_grants(workspace_id,plugin_key,version,user_no,enabled)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,plugin_key,version,user_no)
                    DO UPDATE SET enabled=EXCLUDED.enabled''',
                    (workspace_id,_text(args.plugin,160),_text(args.version,80),_text(args.user),args.enabled))
                action='admin:plugin-member-updated'
            elif args.command=='document':
                cur.execute('''UPDATE platform.hybrid_documents SET enabled=%s,updated_at=now()
                    WHERE id=%s AND workspace_id=%s RETURNING id''',(args.enabled,args.document,workspace_id))
                if not cur.fetchone():
                    raise ValueError('document not found in workspace')
                action='admin:document-enabled' if args.enabled else 'admin:document-withdrawn'
            elif args.command=='workspace':
                cur.execute('UPDATE platform.hybrid_workspaces SET enabled=%s WHERE id=%s',(args.enabled,workspace_id))
                action='admin:workspace-enabled' if args.enabled else 'admin:workspace-disabled'
            else:
                raise ValueError('unknown command')
        cur.execute('''INSERT INTO platform.hybrid_audit(workspace_id,actor_user_no,action,resource_id)
            VALUES(%s,%s,%s,%s)''',(workspace_id,admin,action,args.document if args.command=='document' else workspace_id))
    return {'status':'updated','workspace_id':workspace_id,'action':action}


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--operator',required=True,help='Administrator employee number for audit (OS/DB access is the authority)')
    sub=p.add_subparsers(dest='command',required=True)
    for name in ('create','member','plugin-install','plugin-member','document','workspace'):
        s=sub.add_parser(name)
        s.add_argument('--workspace',type=UUID,required=name!='create')
        if name=='create':
            s.add_argument('--kind',choices=['department','group'],required=True)
            s.add_argument('--organization')
            s.add_argument('--name',required=True)
            s.add_argument('--model',default='deepseek-flash-v4.1')
        if name in ('member','plugin-member'):
            s.add_argument('--user',required=True)
        if name in ('member','plugin-member','document','workspace'):
            s.add_argument('--enabled',action=argparse.BooleanOptionalAction,default=True)
        if name=='member':
            for flag in ('can-chat','can-share','can-publish','can-retrieve'):
                s.add_argument('--'+flag,action=argparse.BooleanOptionalAction,default=False)
        if name in ('plugin-install','plugin-member'):
            s.add_argument('--plugin',required=True)
        if name=='plugin-install':
            s.add_argument('--model')
        if name=='plugin-member':
            s.add_argument('--version',required=True)
        if name=='document':
            s.add_argument('--document',type=UUID,required=True)
    return p


if __name__=='__main__':
    try:
        print(json.dumps(execute(parser().parse_args()),ensure_ascii=False))
    except Exception as exc:
        # Never print a DB exception or configuration, which may contain secrets.
        print('WORKSPACE_ADMIN: FAILED ('+type(exc).__name__+')')
        raise SystemExit(1)
