"""Distribution catalog. PostgreSQL authorizes every read including access by digest."""
import os
import re
from fastapi import HTTPException
from .badge_grants import DEFAULT_BADGE_SQL
from .history_store import _connect
from .plugin_packages import KEY, VERSION
from .skill_network_policy import BUNDLED, CONFIDENTIAL, PUBLIC, _reviews

FIELDS="""r.plugin_key,r.version,p.name,p.description,r.kind,r.sha256,r.state,
          octet_length(r.archive) AS size_bytes,r.created_at,r.updated_at"""
JOIN="""platform.plugin_releases r JOIN platform.enterprise_plugin_catalog p USING(plugin_key,version)"""
# Join the override for this employee only. A stored False always wins.
GRANT_JOIN = """ LEFT JOIN platform.enterprise_plugin_grants g
                 ON g.plugin_key=r.plugin_key AND g.version=r.version AND g.user_no=%s """
EFFECTIVE_GRANT = f"COALESCE(g.enabled, {DEFAULT_BADGE_SQL})"
USER=re.compile(r'M[0-9]{8}')
HASH=re.compile(r'[a-f0-9]{64}')


def event(cur, actor, key, version, action, employee=None, client=None, status=None):
    cur.execute("""INSERT INTO platform.plugin_release_events(actor,plugin_key,version,action,employee,client,status,created_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,clock_timestamp())""",(actor,key,version,action,employee,client,status))


def _clients(kind):
    return ['web','desktop'] if kind=='skill' else ['desktop']


def _native_trusted(digest):
    return digest in set(re.split(r'[,\s]+',os.getenv('MAIMI_TRUSTED_NATIVE_PLUGIN_SHA256','').strip().lower()))


def _release(row):
    return {**dict(row),'clients':_clients(row['kind'])}


def releases():
    with _connect() as c,c.cursor() as cur:
        cur.execute('SELECT '+FIELDS+' FROM '+JOIN+' ORDER BY r.updated_at DESC,r.plugin_key,r.version')
        return [_release(r) for r in cur.fetchall()]


def create_release(admin, package):
    from .plugin_admin import PROJECTS
    manifest, archive, sha, instructions=package
    key, version=manifest['plugin_key'],manifest['version']
    if key in PROJECTS or key=='employee-cards' or 'company-'+key in BUNDLED:
        raise HTTPException(409,'plugin_key_reserved')
    with _connect() as c,c.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('maimi-release:'+key,))
        cur.execute("""SELECT runner FROM platform.enterprise_plugin_catalog WHERE plugin_key=%s""",(key,))
        if any(x['runner']!='maimi-distribution' for x in cur.fetchall()):
            raise HTTPException(409,'plugin_key_reserved')
        cur.execute('SELECT '+FIELDS+' FROM '+JOIN+' WHERE r.plugin_key=%s AND r.version=%s',(key,version))
        previous=cur.fetchone()
        if previous:
            if previous['sha256']!=sha: raise HTTPException(409,'plugin_version_immutable')
            return _release(previous)
        cur.execute("""INSERT INTO platform.enterprise_plugin_catalog(plugin_key,version,name,description,runner,enabled)
                       VALUES(%s,%s,%s,%s,'maimi-distribution',false)""",(key,version,manifest['name'],manifest['description']))
        cur.execute("""INSERT INTO platform.plugin_releases(plugin_key,version,kind,sha256,archive,instructions)
                       VALUES(%s,%s,%s,%s,%s,%s)""",(key,version,manifest['kind'],sha,archive,instructions))
        event(cur,admin,key,version,'upload')
        cur.execute('SELECT '+FIELDS+' FROM '+JOIN+' WHERE r.plugin_key=%s AND r.version=%s',(key,version))
        return _release(cur.fetchone())


def _checks(row):
    checks=[{'id':'format','label':'包格式及文件完整性','ok':True,'message':'上传时已校验'}]
    if row['kind']=='native':
        trusted=_native_trusted(row['sha256'])
        checks.append({'id':'native_trust','label':'原生插件独立审核','ok':trusted,
                       'message':'摘要已登记' if trusted else '请将完整包摘要交运维独立审核并登记'})
    return {'ok':all(i['ok'] for i in checks),'checks':checks}


def change_state(admin,key,version,sha,action,expected_state=None,expected_updated_at=None):
    with _connect() as c,c.cursor() as cur:
        # Take the catalog lock before the release lock, in the same order as grant edits.
        cur.execute('SELECT 1 FROM platform.enterprise_plugin_catalog WHERE plugin_key=%s AND version=%s FOR UPDATE',(key,version))
        cur.execute('SELECT '+FIELDS+' FROM '+JOIN+' WHERE r.plugin_key=%s AND r.version=%s FOR UPDATE OF r',(key,version))
        row=cur.fetchone()
        if not row:raise HTTPException(404,'plugin_release_not_found')
        if row['sha256']!=sha:raise HTTPException(409,'plugin_digest_changed')
        checks=_checks(row)
        if action=='preflight':return checks
        published=action=='publish'
        if published and not checks['ok']:raise HTTPException(409,'plugin_native_review_required')
        state='published' if published else 'withdrawn'
        if row['state']!=state:
            if ((expected_state is not None and expected_state!=row['state']) or
                    (expected_updated_at is not None and expected_updated_at!=row['updated_at'])):
                raise HTTPException(409,'plugin_release_changed')
            if not published and row['state']=='draft':raise HTTPException(409,'plugin_release_not_published')
            cur.execute("""UPDATE platform.plugin_releases SET state=%s,
                updated_at=GREATEST(clock_timestamp(),updated_at+interval '1 microsecond')
                WHERE plugin_key=%s AND version=%s RETURNING updated_at""",(state,key,version))
            row['updated_at']=cur.fetchone()['updated_at'];row['state']=state
            cur.execute('UPDATE platform.enterprise_plugin_catalog SET enabled=%s WHERE plugin_key=%s AND version=%s',(published,key,version))
            event(cur,admin,key,version,action)
        return _release(row)


def _principal(employee,client):
    if not isinstance(employee,str) or not USER.fullmatch(employee):raise HTTPException(403,'employee_identity_required')
    if client not in ('desktop','web'):raise HTTPException(422,'invalid_plugin_client')


def _available(cur,employee,client,key=None,*,network_mode=PUBLIC):
    _principal(employee,client)
    cur.execute('SELECT '+FIELDS+' FROM '+JOIN+GRANT_JOIN+f"""
       WHERE r.state='published' AND p.enabled AND {EFFECTIVE_GRANT} AND (r.kind='skill' OR %s='desktop')
       AND (%s::text IS NULL OR r.plugin_key=%s)
       ORDER BY r.plugin_key,string_to_array(r.version,'.')::int[] DESC""",(employee,client,key,key))
    rows=cur.fetchall()
    reviews=_reviews(cur,{'company-'+r['plugin_key'] for r in rows})
    selected={}
    for row in rows:
        if row['kind']=='native' and not _native_trusted(row['sha256']):continue
        required = reviews.get('company-'+row['plugin_key'],(PUBLIC,0))[0]
        if required==CONFIDENTIAL and network_mode!=CONFIDENTIAL:continue
        item=_release(row);k,v=row['plugin_key'],row['version']
        item['network_mode']=required
        item['package_url']=f'/v1/plugins/distribution/{k}/{v}/package?client={client}'
        selected.setdefault(k,item)
    return selected


def list_for_employee(employee,client):
    _principal(employee,client)
    with _connect() as c,c.cursor() as cur:return list(_available(cur,employee,client).values())


def _read(employee,key,version,sha,client,skill,*,network_mode=PUBLIC):
    _principal(employee,client)
    if not KEY.fullmatch(key) or not VERSION.fullmatch(version):raise HTTPException(404,'plugin_release_unavailable')
    with _connect() as c,c.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', ('maimi-network:company-'+key,))
        row=_available(cur,employee,client,key,network_mode=network_mode).get(key)
        if not row or row['version']!=version or (sha and row['sha256']!=sha) or (skill and row['kind']!='skill'):
            raise HTTPException(404,'plugin_release_unavailable')
        # Recheck grant and state in the statement which actually returns content.
        field='instructions' if skill else 'archive'
        cur.execute('SELECT r.'+field+' AS content FROM '+JOIN+GRANT_JOIN+f"""
            WHERE p.enabled AND {EFFECTIVE_GRANT} AND r.plugin_key=%s AND r.version=%s AND r.sha256=%s AND r.state='published'""",
            (employee,key,version,row['sha256']))
        content=cur.fetchone()
        if not content:raise HTTPException(404,'plugin_release_unavailable')
        if skill:return {**row,'instructions':content['content']}
        return bytes(content['content']),row['sha256']


def skill_for_employee(employee,key,version,sha256,client,*,network_mode=PUBLIC):
    if not isinstance(sha256,str) or not HASH.fullmatch(sha256):raise HTTPException(422,'plugin_digest_required')
    return _read(employee,key,version,sha256,client,True,network_mode=network_mode)


def archive_for_employee(employee,key,version,client):return _read(employee,key,version,None,client,False)


def record_receipt(employee,client,key,version,sha256,status,*,network_mode=PUBLIC,device_id=None,usable=None):
    _principal(employee,client)
    if status not in ('installed','removed','failed'):raise HTTPException(422,'invalid_plugin_receipt')
    if usable is not None and (device_id is None or type(usable) is not bool or (usable and status!='installed')):
        raise HTTPException(422,'invalid_plugin_receipt')
    with _connect() as c,c.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))', ('maimi-network:company-'+key,))
        # Grant and publication updates lock this catalog row first. Concurrent
        # receipts share it so validation, state changes and their audit agree.
        cur.execute('SELECT 1 FROM platform.enterprise_plugin_catalog WHERE plugin_key=%s AND version=%s FOR SHARE',
                    (key,version))
        cur.execute('SELECT kind FROM platform.plugin_releases WHERE plugin_key=%s AND version=%s AND sha256=%s FOR SHARE',(key,version,sha256))
        release=cur.fetchone()
        if not release or client not in _clients(release['kind']):raise HTTPException(404,'plugin_release_unavailable')
        if status=='removed':
            cur.execute("""SELECT 1 FROM platform.plugin_receipts WHERE employee=%s AND client=%s AND plugin_key=%s AND version=%s
                           UNION ALL SELECT 1 FROM platform.enterprise_plugin_grants WHERE user_no=%s AND plugin_key=%s AND version=%s
                           UNION ALL SELECT 1 FROM platform.plugin_device_reports WHERE employee=%s AND client=%s AND plugin_key=%s AND version=%s""",
                        (employee,client,key,version,employee,key,version,employee,client,key,version))
            if not cur.fetchone():raise HTTPException(403,'plugin_receipt_scope')
        else:
            row=_available(cur,employee,client,key,network_mode=network_mode).get(key)
            if not row or row['version']!=version:raise HTTPException(403,'plugin_receipt_scope')
        cur.execute("""INSERT INTO platform.plugin_receipts(employee,client,plugin_key,version,status,created_at)
            VALUES(%s,%s,%s,%s,%s,clock_timestamp())
            ON CONFLICT(employee,client,plugin_key,version) DO UPDATE SET status=EXCLUDED.status,created_at=clock_timestamp()
            WHERE platform.plugin_receipts.status IS DISTINCT FROM EXCLUDED.status RETURNING status""",(employee,client,key,version,status))
        if cur.fetchone():event(cur,employee,key,version,'receipt',employee,client,status)
        else:
            # An old client may acknowledge the same state after a revoke/regrant.
            cur.execute("""UPDATE platform.plugin_receipts a SET created_at=clock_timestamp()
              FROM platform.plugin_releases r LEFT JOIN platform.enterprise_plugin_grants g
              ON g.plugin_key=r.plugin_key AND g.version=r.version AND g.user_no=%s
              WHERE a.employee=%s AND a.client=%s AND a.plugin_key=%s AND a.version=%s
                AND r.plugin_key=a.plugin_key AND r.version=a.version
                AND a.created_at < GREATEST(r.updated_at,g.changed_at)""",(employee,employee,client,key,version))
        if device_id is not None:
            cur.execute("""INSERT INTO platform.plugin_device_reports
                (employee,client,device_id,plugin_key,version,status,usable)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(employee,client,plugin_key,version,device_id)
                DO UPDATE SET status=EXCLUDED.status,usable=EXCLUDED.usable,reported_at=clock_timestamp()""",
                (employee,client,device_id,key,version,status,usable))
        return {'saved':True,'status':status}


def activity(employee):
    with _connect() as c,c.cursor() as cur:
        cur.execute("""SELECT created_at,actor,employee,plugin_key,version,action,client,status FROM platform.plugin_release_events
          WHERE (%s::text IS NULL OR employee=%s) ORDER BY id DESC LIMIT 100""",(employee or None,employee or None))
        return list(cur.fetchall())

def distribution_for_employee(employee,client,*,network_mode=PUBLIC):
    _principal(employee,client)
    with _connect() as conn,conn.cursor() as cur:
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        # All browser workspaces for an employee report into one receipt stream.
        # Advertise only this workspace's skills, but revoke a receipt only when
        # no permitted browser workspace can still use that exact release.
        all_available=_available(cur,employee,client,network_mode=CONFIDENTIAL if client=='web' else network_mode)
        available={key:row for key,row in all_available.items() if
                   row['network_mode']==PUBLIC or network_mode==CONFIDENTIAL}
        cur.execute("""SELECT a.plugin_key,a.version,r.sha256 FROM (
          SELECT plugin_key,version FROM platform.plugin_receipts
            WHERE employee=%s AND client=%s AND status IN ('installed','failed')
          UNION SELECT plugin_key,version FROM platform.plugin_device_reports
            WHERE employee=%s AND client=%s AND status IN ('installed','failed')
          ) a JOIN platform.plugin_releases r USING(plugin_key,version)
          ORDER BY a.plugin_key,a.version""",(employee,client,employee,client))
        removed=[dict(row) for row in cur.fetchall() if (all_available.get(row['plugin_key']) or {}).get('version')!=row['version']]
        return {'items':list(available.values()),'remove':removed}


def removed_candidates_for_employee(employee,client):
    return distribution_for_employee(employee,client)['remove']
