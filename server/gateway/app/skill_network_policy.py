"""Trusted, administrator-reviewed network requirements for cloud skills."""
import json
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL,uuid5
from fastapi import HTTPException

from .history_store import _connect

PUBLIC = 'public'
CONFIDENTIAL = 'confidential'
ACTION = 'web_network_review'
# Each key is the runtime skill ID. Project authorization uses connector IDs.
PROJECT_NAMES = {'meeting': 'meeting-documents', 'heatcost': 'drawing-cost-analysis',
                 'service': 'server-operations', 'infrastructure-monitor': 'infrastructure-monitor',
                 'sales-orders': 'sales-orders', 'sales-order-approval': 'sales-order-approval'}
BUNDLED = {'enterprise-tools': '企业任务路由', 'company-ppt-template': '公司演示文稿模板',
           'patent-review': '专利审核', 'onboarding-health-review': '入职体检报告辅助复核'}
# A colon is outside the uploadable package key alphabet. A packaged skill and
# a built-in with the same display name can therefore be assigned independently.
BUNDLED_GRANTS = {'builtin:' + skill: skill for skill in BUNDLED}


def _reviews(cur, names=None):
    # These records are permanent, append-only policy decisions. The gateway
    # role can SELECT/INSERT events, but cannot UPDATE or DELETE them.
    sql = """SELECT DISTINCT ON (plugin_key) plugin_key, status, id
             FROM platform.plugin_release_events
             WHERE employee IS NULL AND client='web' AND version='*' AND action=%s"""
    params = [ACTION]
    if names is not None:
        if not names:
            return {}
        sql += " AND plugin_key=ANY(%s)"
        params.append(sorted(names))
    cur.execute(sql + ' ORDER BY plugin_key,id DESC', params)
    result = {}
    for row in cur.fetchall():
        if row['status'] not in (PUBLIC, CONFIDENTIAL):
            raise HTTPException(503, 'skill_network_policy_invalid')
        result[row['plugin_key']] = (row['status'], row['id'])
    return result


def _catalog(cur):
    from .plugin_admin import PROJECTS
    names = {**BUNDLED, **{key: PROJECTS[connector] for connector, key in PROJECT_NAMES.items()}}
    cur.execute("""SELECT DISTINCT ON (r.plugin_key) r.plugin_key,p.name
        FROM platform.plugin_releases r JOIN platform.enterprise_plugin_catalog p USING(plugin_key,version)
        WHERE r.kind IN ('skill','native') ORDER BY r.plugin_key,r.created_at DESC,r.version""")
    for row in cur.fetchall():
        names.setdefault('company-' + row['plugin_key'], row['name'])
    return names


def policy_catalog():
    with _connect() as conn, conn.cursor() as cur:
        names = _catalog(cur)
        reviews = _reviews(cur)
        project_keys = {skill: project for project, skill in PROJECT_NAMES.items()}
        project_keys['company-employee-badges']='employee-badges'
        return [dict(skill=key, name=label, project_key=project_keys.get(key),
                     network_mode=reviews.get(key,(PUBLIC,0))[0],
                     revision=reviews.get(key,(PUBLIC,0))[1]) for key,label in sorted(names.items())]


def review(admin, skill, mode, expected_revision):
    if mode not in (PUBLIC, CONFIDENTIAL):
        raise HTTPException(422, 'skill_network_mode_invalid')
    with _connect() as conn, conn.cursor() as cur:
        if skill not in _catalog(cur):
            raise HTTPException(404, 'skill_network_target_unknown')
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('maimi-network:'+skill,))
        current, revision = _reviews(cur, {skill}).get(skill, (PUBLIC, 0))
        if revision != expected_revision:
            raise HTTPException(409, 'skill_network_review_changed')
        if current != mode:
            cur.execute("""INSERT INTO platform.plugin_release_events
                (actor,plugin_key,version,action,client,status,created_at)
                VALUES(%s,%s,'*',%s,'web',%s,clock_timestamp()) RETURNING id""",
                        (admin,skill,ACTION,mode))
            revision = cur.fetchone()['id']
        return dict(skill=skill, network_mode=mode, revision=revision)


def reviewed_confidential_names(skill_names):
    """Return the confidential subset in the caller's original connector/skill vocabulary."""
    if isinstance(skill_names, str):
        skill_names = [skill_names]
    names = {name: ('company-employee-badges' if name == 'employee-badges' else PROJECT_NAMES.get(name,name)) for name in skill_names}
    if not names:
        return set()
    with _connect() as conn, conn.cursor() as cur:
        decisions = _reviews(cur,set(names.values()))
    return {original for original, canonical in names.items() if decisions.get(canonical,(PUBLIC,0))[0] == CONFIDENTIAL}


def resolve_skill_network_mode(skill_names):
    """Resolve server-known DSH names or project connector keys. Grants are separate."""
    return CONFIDENTIAL if reviewed_confidential_names(skill_names) else PUBLIC


def _visible_bundled(cur, employee):
    from .plugin_admin import VERSION
    from .project_platform import SERVICE_USERS
    cur.execute("""SELECT plugin_key FROM platform.enterprise_plugin_grants
                   WHERE user_no=%s AND version=%s AND NOT enabled""", (employee,VERSION))
    denied = {r['plugin_key'] for r in cur.fetchall()}
    if employee not in SERVICE_USERS:
        denied.update(('service','infrastructure-monitor'))
    bundled = {skill for grant, skill in BUNDLED_GRANTS.items() if grant not in denied}
    return bundled | {name for key,name in PROJECT_NAMES.items() if key not in denied}


def bundled_skills_for_workspace(employee, network_mode):
    from .plugin_distribution import _principal
    _principal(employee,'web')
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        visible = _visible_bundled(cur,employee)
        reviews = _reviews(cur,visible)
        return sorted(name for name in visible if network_mode == CONFIDENTIAL or
                      reviews.get(name,(PUBLIC,0))[0] == PUBLIC)


def additional_picker_skills(employee):
    """Names for the ordinary skill menu, without loading instructions into an online worker."""
    from . import plugin_distribution as dist
    dist._principal(employee,'web')
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        private = {name for name,(mode,_) in _reviews(cur).items() if mode == CONFIDENTIAL}
        if not private:
            return []
        visible = _visible_bundled(cur,employee)
        visible.update('company-'+key for key in dist._available(cur, employee, 'web', network_mode=CONFIDENTIAL))
        names = _catalog(cur)
        return [dict(name=key,description=names[key],modelInvocable=False)
                for key in sorted(visible & private & names.keys())]


def confidential_skills_for_employee(employee):
    return [row['name'] for row in additional_picker_skills(employee)]

def additional_project_picker(employee):
    from .plugin_admin import PROJECTS
    visible=set(confidential_skills_for_employee(employee))
    return [dict(plugin_key=key,name=PROJECTS[key],status='available',tools=[])
            for key,skill in PROJECT_NAMES.items() if skill in visible]

@lru_cache(maxsize=1)
def _bundled_instructions():
    return json.loads(Path(__file__).with_name('bundled_task_instructions.json').read_text('utf-8'))

def task_instructions(employee,names,owner=None):
    """Read only selected, currently granted company instructions for the bound model call."""
    from . import plugin_distribution as dist
    dist._principal(employee,'web')
    names={('company-employee-badges' if k == 'employee-badges' else PROJECT_NAMES.get(k,k)) for k in names}
    if not names:return []
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        private={name for name,(mode,_) in _reviews(cur,names).items() if mode==CONFIDENTIAL}
        if not private:return []
        visible=_visible_bundled(cur,employee)
        released=dist._available(cur,employee,'web',network_mode=CONFIDENTIAL)
        selected=[]
        for name in sorted(private):
            if name in visible:
                instructions=_bundled_instructions().get(name)
            else:
                row=released.get(name.removeprefix('company-')) if name.startswith('company-') else None
                if not row or row['kind']!='skill':
                    raise HTTPException(403,'skill_unavailable')
                # Query approval, employee grant and exact deployed version again at content read.
                instructions=dist.skill_for_employee(employee,row['plugin_key'],row['version'],row['sha256'],
                                                       'web',network_mode=CONFIDENTIAL)['instructions']
                if owner and instructions:
                    device=uuid5(NAMESPACE_URL,'maimi.web.plugin-availability/'+owner)
                    dist.record_receipt(employee,'web',row['plugin_key'],row['version'],row['sha256'],
                                        'installed',network_mode=CONFIDENTIAL,device_id=device,usable=True)
            if not isinstance(instructions,str) or not instructions.strip():raise HTTPException(503,'skill_unavailable')
            selected.append((name,instructions))
        return selected
