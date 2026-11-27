"""Administer released plugins using the same grants that gate employee requests."""
from app.site_access import configured_staff_group

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from .portal_assets import PLUGIN, asset_response, version_links
from .badge_grants import BADGE_GRANT_QUERY, BADGE_KEY, default_badge_grant
from .history_store import _connect
from .skill_network_policy import BUNDLED, BUNDLED_GRANTS

router = APIRouter()
ASSETS = Path(__file__).with_name('plugin_admin_assets')
USER = re.compile(r'M[0-9]{8}')
_owner = os.environ.get("MAIMI_PLUGIN_OWNER", "").strip()
OWNER = _owner if re.fullmatch(r"M[0-9]{8}", _owner) else None
INITIAL_MANAGERS = frozenset(configured_staff_group('MAIMI_INITIAL_PLUGIN_MANAGERS'))
PROJECTS = {'meeting':'会议文档','heatcost':'图纸成本分析',
            'infrastructure-monitor':'基础设施监控','service':'服务器运维','sales-orders':'销售订单识别与归档',
            'sales-order-approval':'销售订单审核'}
VERSION = '1.0.1'
DEFAULT_GRANTS = {**PROJECTS, **{key: BUNDLED[skill] for key, skill in BUNDLED_GRANTS.items()}}
HEADERS = {'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
           'Content-Security-Policy':"default-src 'none'; img-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}
class Change(BaseModel):
    model_config = ConfigDict(extra='forbid')
    employee: str
    plugin: str
    version: str
    enabled: StrictBool
    expected_enabled: StrictBool | None = None

class CatalogPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    employees: list[str] = Field(min_length=1,max_length=200)

class RoleChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    employee: str = Field(pattern=r'^M[0-9]{8}$')
    enabled: StrictBool
    expected_revision: int = Field(ge=0,strict=True)

def project_granted(user_no, plugin_key):
    if plugin_key == BADGE_KEY and USER.fullmatch(user_no or ''):
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(BADGE_GRANT_QUERY,(user_no,))
            return cur.fetchone() is not None
    if plugin_key not in PROJECTS or not USER.fullmatch(user_no or ''):
        return False
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT enabled FROM platform.enterprise_plugin_grants WHERE user_no=%s AND plugin_key=%s AND version=%s',
                    (user_no,plugin_key,VERSION))
        row=cur.fetchone()
    return bool(row['enabled']) if row else True

def employee_identity(request):
    user=getattr(request.state,'user',None)
    if not isinstance(user,dict) or user.get('auth_provider')!='iam_direct' or not USER.fullmatch(user.get('user_no') or ''):
        raise HTTPException(401,'iam_employee_required')
    return user['user_no']

def role_in_transaction(cur, employee):
    if OWNER is not None and employee == OWNER:
        return {'employee':employee,'role':'owner','revision':0}
    cur.execute("""SELECT id,status FROM platform.plugin_release_events
        WHERE employee=%s AND actor=%s AND action='plugin_admin_role' AND plugin_key='role:plugin-admin'
          AND version='*' AND client IS NULL ORDER BY id DESC LIMIT 1""", (employee,OWNER))
    event = cur.fetchone()
    if event and event['status'] not in ('granted','revoked'):
        raise HTTPException(503,'plugin_admin_role_unavailable')
    granted = event['status'] == 'granted' if event else employee in INITIAL_MANAGERS
    return {'employee':employee,'role':'manager' if granted else 'reader','revision':event['id'] if event else 0}

def lookup_role(employee):
    with _connect() as conn, conn.cursor() as cur:
        return role_in_transaction(cur, employee)

def lock_role(cur, employee, *, shared=False):
    lock = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
    cur.execute('SELECT '+lock+'(hashtextextended(%s,0))', ('maimi-role:plugin-admin:'+employee,))

def store_role_change(body):
    if body.employee == OWNER:
        raise HTTPException(409,'plugin_owner_immutable')
    with _connect() as conn,conn.cursor() as cur:
        lock_role(cur,body.employee)
        prior = role_in_transaction(cur, body.employee)
        if prior['revision'] != body.expected_revision:
            raise HTTPException(409,'plugin_admin_role_changed')
        if (prior['role']=='manager') == body.enabled:
            return prior
        cur.execute("""INSERT INTO platform.plugin_release_events(actor,employee,plugin_key,version,action,status)
            VALUES(%s,%s,'role:plugin-admin','*','plugin_admin_role',%s) RETURNING id""",
            (OWNER,body.employee,'granted' if body.enabled else 'revoked'))
        return {'employee':body.employee,'revision':cur.fetchone()['id'],'role':'manager' if body.enabled else 'reader'}

def configured_admin(request):
    employee = employee_identity(request)
    if employee != OWNER and lookup_role(employee)['role'] != 'manager':
        raise HTTPException(403,'plugin_admin_required')
    require_same_origin(request)
    return employee

def require_same_origin(request):
    if request.method=='POST' and (not os.getenv('IAM_PUBLIC_ORIGIN') or request.headers.get('origin')!=os.getenv('IAM_PUBLIC_ORIGIN') or request.headers.get('sec-fetch-site')=='cross-site'):
        raise HTTPException(403,'plugin_admin_origin_denied')

def require_owner(request):
    if OWNER is None or employee_identity(request) != OWNER:
        raise HTTPException(403,'plugin_owner_required')
    require_same_origin(request)

def inventory(employee,preview_employees=()):
    from .project_platform import SERVICE_USERS
    distributable = {}
    device_reports = {}
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        if preview_employees:
            cur.execute("""WITH counts AS (
                SELECT plugin_key,version,count(*) FILTER (WHERE enabled) AS granted,
                       count(*) FILTER (WHERE NOT enabled) AS revoked,
                       count(*) FILTER (WHERE NOT enabled AND user_no=ANY(%s)) AS eligible_revoked
                FROM platform.enterprise_plugin_grants WHERE user_no=ANY(%s)
                GROUP BY plugin_key,version
            )
            SELECT p.plugin_key,p.version,p.name,r.kind,r.state,p.enabled AS catalog_enabled,
                   CASE WHEN NOT p.enabled AND counts.granted>0 THEN true END AS enabled,
                   COALESCE(counts.granted,0) AS explicit_grants,
                   COALESCE(counts.revoked,0) AS explicit_revokes,
                   COALESCE(counts.eligible_revoked,0) AS eligible_revoked
            FROM platform.enterprise_plugin_catalog p
            LEFT JOIN platform.plugin_releases r USING(plugin_key,version)
            LEFT JOIN counts USING(plugin_key,version)
            WHERE (p.enabled OR (r.state='withdrawn' AND counts.granted>0))
              AND p.plugin_key <> 'employee-cards' AND p.runner NOT LIKE 'synthetic-%%'
            ORDER BY p.name,p.version""",(sorted(SERVICE_USERS),preview_employees))
        else:
            cur.execute("""SELECT p.plugin_key,p.version,p.name,g.enabled,r.kind,r.state,p.enabled AS catalog_enabled,
GREATEST(g.changed_at,r.updated_at,history.changed_at) AS effective_since FROM platform.enterprise_plugin_catalog p
LEFT JOIN platform.plugin_releases r USING(plugin_key,version)
LEFT JOIN platform.enterprise_plugin_grants g ON g.plugin_key=p.plugin_key AND g.version=p.version AND g.user_no=%s
LEFT JOIN LATERAL (
    SELECT created_at AS changed_at FROM platform.plugin_release_events e WHERE e.employee=%s
        AND e.plugin_key=p.plugin_key AND e.version=p.version AND e.action IN ('grant','revoke')
    ORDER BY e.id DESC LIMIT 1
) history ON g.changed_at IS NULL
WHERE (p.enabled OR (g.enabled AND r.state='withdrawn'))
  AND p.plugin_key <> 'employee-cards' AND p.runner NOT LIKE 'synthetic-%%' ORDER BY p.name,p.version""",(employee,employee))
        rows={(r['plugin_key'],r['version']):dict(r) for r in cur.fetchall()}
        if employee:
            cur.execute("""SELECT * FROM (
                SELECT plugin_key,version,client,status,created_at,NULL::boolean AS usable,0 AS priority
                FROM platform.plugin_receipts WHERE employee=%s
                UNION ALL
                SELECT plugin_key,version,client,status,reported_at,usable,1
                FROM platform.plugin_device_reports WHERE employee=%s
                ) AS reports ORDER BY plugin_key,version,client,created_at DESC,priority DESC""",(employee,employee))
            for report in cur.fetchall():
                item=rows.get((report['plugin_key'],report['version']))
                if item is not None:
                    receipt={key:report[key] for key in ('status','created_at','usable')}
                    receipts=item.setdefault('receipts',{'web':None,'desktop':None})
                    if receipts[report['client']] is None:
                        receipts[report['client']]=receipt
                    if report['priority']:
                        device_reports.setdefault((report['plugin_key'],report['version'],report['client']),[]).append(receipt)
            if any(r.get('kind') for r in rows.values()):
                from .plugin_distribution import _available
                from .skill_network_policy import CONFIDENTIAL, PUBLIC
                for client, mode in (('web',CONFIDENTIAL),('desktop',PUBLIC)):
                    distributable[client] = {(r['plugin_key'],r['version']) for r in _available(cur,employee,client,network_mode=mode).values()}
    for key,name in DEFAULT_GRANTS.items():
        rows.setdefault((key,VERSION),dict(plugin_key=key,version=VERSION,name=name,enabled=None))
    for row in rows.values():
        key = row['plugin_key']
        if key in BUNDLED_GRANTS and row['version'] == VERSION:
            row.update(kind='skill', skill=BUNDLED_GRANTS[key], delivery='bundled_skill', clients=['web'], receipts=None)
        elif row.get('kind'):
            row['delivery'] = 'package'
            row['clients'] = ['desktop'] if row['kind'] == 'native' else ['web', 'desktop']
            row.setdefault('receipts', {'web': None, 'desktop': None})
        elif key in PROJECTS and row['version'] == VERSION:
            row.update(delivery='enterprise_workspace', clients=['web'], receipts=None)
        else:
            row.update(delivery='catalog', clients=[], receipts=None)
        catalog_enabled = row.pop('catalog_enabled',False)
        default_enabled = (key in DEFAULT_GRANTS and row['version']==VERSION) or default_badge_grant(
            key,row['version'],row.get('kind'),row.get('state'),catalog_enabled)
        granted = bool(row['enabled']) if row['enabled'] is not None else default_enabled
        row['effective'] = granted and row.get('state') != 'withdrawn'
        row['restricted'] = key in {'infrastructure-monitor', 'service'}
        row['job_limited'] = bool(row['restricted'] and employee and employee not in SERVICE_USERS)
        if row['job_limited']:
            row['effective'] = False
        row['authorization'] = ('granted' if granted else 'revoked') if employee else None
        row['client_states'] = {client: client_state(row,client,(key,row['version']) in distributable.get(client,()),
                                                    device_reports.get((key,row['version'],client),[]))
                                for client in ('web','desktop')} if employee else {}
        if preview_employees:
            granted_count = row.pop('explicit_grants',0)
            revoked_count = row.pop('explicit_revokes',0)
            eligible_revoked = row.pop('eligible_revoked',0)
            if row.get('state') == 'withdrawn':
                row['grant_totals'] = {'on':0,'off':0,'retained':granted_count}
            else:
                if default_enabled:
                    granted_count = len(preview_employees) - revoked_count
                row['grant_totals'] = {'on':granted_count,'off':len(preview_employees)-granted_count,'retained':0}
                if row['restricted'] and row['version']==VERSION:
                    eligible = len(SERVICE_USERS.intersection(preview_employees)) - eligible_revoked
                    row['grant_totals'].update(on=eligible,off=len(preview_employees)-eligible,
                                               job_limited=granted_count-eligible)
        row.pop('effective_since',None)
    return list(rows.values())

def client_state(row, client, distributable, devices=()):
    if client not in row['clients']:
        return {'sync':'not_applicable','availability':'not_applicable','at':None}
    if row['delivery'] != 'package':
        return {'sync':'not_applicable','availability':'unverified' if row['effective'] else 'unavailable','at':None}
    report = (row.get('receipts') or {}).get(client)
    if not devices:
        return receipt_state(report, row.get('effective_since'), distributable)
    states=[receipt_state(item,row.get('effective_since'),distributable) for item in devices]
    sync={s['sync'] for s in states}
    available={s['availability'] for s in states}
    return {
        'sync':next(iter(sync)) if len(sync)==1 else 'partial',
        'availability':next(iter(available)) if len(available)==1 else 'partial' if 'available' in available else 'unverified',
        'at':max(s['at'] for s in states),
        'devices':len(states),'synced_devices':sum(s['sync']=='synced' for s in states),
        'failed_devices':sum(s['sync']=='failed' for s in states),
    }

def receipt_state(report,effective_since,distributable):
    at = report['created_at'] if report else None
    fresh = bool(report and (not effective_since or at >= effective_since))
    desired = 'installed' if distributable else 'removed'
    sync = 'pending'
    availability = 'unverified' if distributable else 'unavailable'
    if fresh:
        if report['status'] == desired:
            sync = 'synced'
            if desired == 'installed' and report.get('usable') is not None:
                availability = 'available' if report['usable'] else 'unavailable'
        elif report['status'] == 'failed':
            sync, availability = 'failed', 'unavailable'
    return {'sync':sync,'availability':availability,'at':at}

def change(body, admin=None):
    with _connect() as conn,conn.cursor() as cur:
        return change_in_transaction(cur,body,admin)

def change_in_transaction(cur,body,admin=None,decision_at=None):
    if body.plugin == 'employee-cards':
        raise HTTPException(404, 'plugin_release_not_found')
    if not USER.fullmatch(body.employee) or not (1<=len(body.plugin)<=160 and 1<=len(body.version)<=80):
        raise HTTPException(422,'invalid_assignment')
    builtin = body.plugin in DEFAULT_GRANTS and body.version == VERSION
    if builtin:
        runner = 'maimi-bundled-skill' if body.plugin in BUNDLED_GRANTS else 'maimi-project'
        cur.execute("""INSERT INTO platform.enterprise_plugin_catalog(plugin_key,version,name,runner)
            VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (body.plugin,body.version,DEFAULT_GRANTS[body.plugin],runner))
    # Publication, dashboard edits and receipt validation all lock this row first.
    cur.execute("""SELECT p.enabled,r.state,r.kind FROM platform.enterprise_plugin_catalog p
        LEFT JOIN platform.plugin_releases r USING(plugin_key,version)
        WHERE p.plugin_key=%s AND p.version=%s AND p.runner NOT LIKE 'synthetic-%%'
        FOR UPDATE OF p""",(body.plugin,body.version))
    catalog = cur.fetchone()
    if not catalog or (not catalog['enabled'] and (body.enabled or catalog['state'] != 'withdrawn')):
        raise HTTPException(404,'plugin_release_not_found')
    if body.enabled and catalog['state'] not in (None,'published'):
        raise HTTPException(404,'plugin_release_not_found')
    cur.execute("""SELECT enabled,decision_at FROM platform.enterprise_plugin_grants
        WHERE user_no=%s AND plugin_key=%s AND version=%s""",(body.employee,body.plugin,body.version))
    row = cur.fetchone()
    if not catalog['enabled'] and not row and not decision_at:
        raise HTTPException(404,'plugin_release_not_found')
    default_enabled = builtin or default_badge_grant(body.plugin,body.version,catalog['kind'],
                                                  catalog['state'],catalog['enabled'])
    current = bool(row['enabled']) if row else default_enabled
    if decision_at and row and row['decision_at'] and row['decision_at'] > decision_at and current != body.enabled:
        raise HTTPException(409,'plugin_assignment_changed')
    if body.expected_enabled is not None and current != body.expected_enabled:
        raise HTTPException(409,'plugin_assignment_changed')
    cur.execute("""INSERT INTO platform.enterprise_plugin_grants(plugin_key,version,user_no,enabled,decision_at,changed_at)
        VALUES(%s,%s,%s,%s,coalesce(%s,clock_timestamp()),CASE WHEN %s THEN clock_timestamp() END)
        ON CONFLICT(plugin_key,version,user_no) DO UPDATE SET enabled=EXCLUDED.enabled,
          decision_at=GREATEST(platform.enterprise_plugin_grants.decision_at,EXCLUDED.decision_at),
          changed_at=COALESCE(EXCLUDED.changed_at,platform.enterprise_plugin_grants.changed_at)""",
        (body.plugin,body.version,body.employee,body.enabled,decision_at,current!=body.enabled))
    if admin is not None and current!=body.enabled:
        from .plugin_distribution import event
        event(cur,admin,body.plugin,body.version,'grant' if body.enabled else 'revoke',employee=body.employee)
    return current != body.enabled


@router.get('/maimi/plugins/',include_in_schema=False)
@router.get('/maimi/plugins/assets/{name}',include_in_schema=False)
def page(request:Request, name='index.html'):
    types={'index.html':'text/html','app.js':'text/javascript','app.css':'text/css','entry.css':'text/css'}
    if name not in types: raise HTTPException(404)
    if name == 'index.html':
        return Response(version_links((ASSETS / name).read_text('utf8')), media_type=types[name], headers=HEADERS)
    return asset_response(request, PLUGIN + name, types[name], HEADERS)

@router.get('/v1/plugin-admin/catalog')
async def catalog(request:Request,employee:str=''):
    actor = employee_identity(request)
    owner = OWNER is not None and actor == OWNER
    manager = owner or (await asyncio.to_thread(lookup_role,actor))['role'] == 'manager'
    if employee and not manager:raise HTTPException(403,'plugin_admin_required')
    if employee and not USER.fullmatch(employee):raise HTTPException(422,'invalid_employee')
    items = await asyncio.to_thread(inventory,employee)
    if not manager:
        fields = ('plugin_key','version','name','kind','delivery','clients')
        items = [{key:item[key] for key in fields if key in item}
                 for item in items if item.get('state') in (None,'published')]
    return {'items':items,'employee':employee,'can_manage':manager,'can_manage_roles':owner}

@router.get('/v1/plugin-admin/roles/{employee}')
def employee_role(employee:str,request:Request):
    require_owner(request)
    if not USER.fullmatch(employee): raise HTTPException(422,'invalid_employee')
    return lookup_role(employee)

@router.post('/v1/plugin-admin/roles')
def change_employee_role(body:RoleChange,request:Request):
    require_owner(request)
    return store_role_change(body)

@router.post('/v1/plugin-admin/catalog-preview')
async def preview(body:CatalogPreview,request:Request):
    configured_admin(request)
    employees = list(dict.fromkeys(body.employees))
    if any(not USER.fullmatch(employee) for employee in employees):
        raise HTTPException(422,'invalid_employee')
    return {'items':await asyncio.to_thread(inventory,'',employees),'employees':employees}

@router.post('/v1/plugin-admin/grants')
async def update(body:Change,request:Request):
    admin=configured_admin(request)
    await asyncio.to_thread(change,body,admin)
    logging.getLogger(__name__).info('plugin grant changed admin=%s employee=%s plugin=%s enabled=%s',admin,body.employee,body.plugin,body.enabled)
    return {'saved':True}

from .plugin_release_routes import router as plugin_release_router
router.include_router(plugin_release_router)
from .plugin_batches import router as batch_router
router.include_router(batch_router)
