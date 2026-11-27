"""Read-only administrator view. Does not grant Kubernetes or shell access."""
import asyncio
import json
import math
import os
import stat
import time
from collections import defaultdict, deque
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from ..portal_assets import MONITOR, asset_response, version_links
from .monitor import actual_iam, DEVELOPERS, Denied, HEADERS, USER

SAMPLE = Path('/run/maimi-monitor/cluster.json')
ASSETS = Path(__file__).parent / 'cluster-assets'
QUEUE_WARNING_SECONDS = 30
QUEUE_CRITICAL_SECONDS = 120


def sample_view(path=SAMPLE, now=None):
    if os.environ.get('MAIMI_MONITOR_ENVIRONMENT') == 'PRODUCTION':
        from .production_monitor import sample_view as production_view
        return production_view(now=now)
    now = time.time() if now is None else now
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            s = os.fstat(stream.fileno())
            if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022 or s.st_size > 1024 * 1024:
                raise ValueError('sample_metadata')
            value = json.loads(stream.read(1024 * 1024 + 1))
        if value.get('schema') != 'maimi-cluster-sample/v1':
            raise ValueError('sample_schema')
        at = value['sampled_at']
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 < at <= now + 5:
            raise ValueError('sample_time')
        age = max(0, now - at)
        value['freshness'] = {'state': 'fresh' if age <= 90 else 'stale', 'age_seconds': int(age)}
        if age > 90:
            value['alerts'] = [{'code': 'COLLECTOR_STALE', 'severity': 'critical', 'scope': 'collector'}] + value['alerts']
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {'schema': 'maimi-cluster-sample/v1', 'freshness': {'state': 'unavailable'},
                'nodes': [], 'services': [], 'coverage': {},
                'alerts': [{'code': 'COLLECTOR_UNAVAILABLE', 'severity': 'critical', 'scope': 'collector'}]}


def database_view():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=3, row_factory=dict_row) as connection, connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute("SET LOCAL statement_timeout='3s'")
        cursor.execute("SET LOCAL lock_timeout='500ms'")
        cursor.execute('SELECT pg_is_in_recovery() AS standby, pg_database_size(current_database()) AS bytes, '
                       '(SELECT count(*) FROM pg_stat_activity) AS connections, '
                       "current_setting('max_connections')::integer AS max_connections")
        result = dict(cursor.fetchone())
        result['status'] = 'available'
        pools = {}
        for name, schema in [('chat', 'platform'), ('execution', 'execution')]:
            # Schema names are fixed code. No task text or employee row is read.
            cursor.execute(f"SELECT state,count(*) AS count FROM {schema}.worker_runtime_reservations WHERE state<>'RELEASED' GROUP BY state")
            states = {r['state']: r['count'] for r in cursor.fetchall()}
            cursor.execute(f"SELECT count(*) AS waiting, EXTRACT(epoch FROM clock_timestamp()-min(created_at)) AS oldest_seconds "
                           f"FROM {schema}.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()")
            row = dict(cursor.fetchone())
            row['oldest_seconds'] = float(row['oldest_seconds']) if row['oldest_seconds'] is not None else 0
            pools[name] = {'states': states, **row}
        result['pools'] = pools
        return result


def database_alerts(database):
    """Aggregate-only alerts; never inspect task text, employee rows or commands."""
    if database.get('status') != 'available':
        return []
    alerts = []
    used, maximum = database.get('connections'), database.get('max_connections')
    if type(used) is int and type(maximum) is int and maximum > 0 and used / maximum >= .8:
        alerts.append({'code': 'DATABASE_CONNECTIONS_HIGH', 'scope': 'database',
            'severity': 'critical' if used / maximum >= .95 else 'warning', 'percent': round(used * 100 / maximum, 1)})
    for name, pool in database.get('pools', {}).items():
        waiting, age = pool.get('waiting'), pool.get('oldest_seconds')
        if type(waiting) is not int or waiting < 0 or type(age) not in (int, float) or not math.isfinite(age) or age < 0:
            alerts.append({'code': 'QUEUE_METRICS_UNAVAILABLE', 'scope': name, 'severity': 'warning'})
            continue
        if waiting and (age >= QUEUE_WARNING_SECONDS or waiting >= 5):
            alerts.append({'code': 'QUEUE_WAIT_HIGH', 'scope': name,
                'severity': 'critical' if age >= QUEUE_CRITICAL_SECONDS or waiting >= 20 else 'warning',
                'waiting': waiting, 'oldest_seconds': round(age)})
        if pool.get('states', {}).get('UNCERTAIN', 0):
            alerts.append({'code': 'WORKER_OUTCOME_UNCERTAIN', 'scope': name, 'severity': 'warning'})
    return alerts


def storage_view():
    from app.storage_lifecycle import default_store
    store = default_store()
    return store.admin_usage() if hasattr(store, 'admin_usage') else {'status': 'not_instrumented'}


async def employee_overview():
    # Never load per-user jobs, storage rankings or history into the employee response.
    sample, db = await asyncio.gather(asyncio.to_thread(sample_view),
                                      asyncio.to_thread(database_view), return_exceptions=True)
    sample = sample if isinstance(sample, dict) else {}
    db = db if isinstance(db, dict) else {}
    freshness = sample.get('freshness', {}).get('state')
    if freshness not in ('fresh', 'stale'):
        freshness = 'unavailable'
    status = sample.get('local_app', {}).get('status')
    if freshness != 'fresh' or status not in ('application_ready', 'available', 'unhealthy'):
        status = 'unknown'
    queues = {}
    for pool in ('chat', 'execution'):
        value = db.get('pools', {}).get(pool, {}).get('waiting')
        queues[pool] = value if db.get('status') == 'available' and type(value) is int and value >= 0 else None
    sampled = sample.get('sampled_at')
    sampled = sampled if type(sampled) in (float, int) and math.isfinite(sampled) and sampled > 0 else None
    return {'freshness': freshness, 'sampled_at': sampled, 'app_status': status, 'waiting': queues}


async def report():
    # Independent sources run together. A database delay must not serially delay
    # the two observer reads and file accounting as well.
    production = os.environ.get('MAIMI_MONITOR_ENVIRONMENT') == 'PRODUCTION'
    sources = [sample_view, database_view, storage_view]
    if production:
        from .owner_monitor import collect
        sources.append(collect)
    results = await asyncio.gather(*(asyncio.to_thread(f) for f in sources), return_exceptions=True)
    view = results[0]
    if isinstance(view, BaseException):
        view = {'environment':'PRODUCTION' if production else 'TEST', 'freshness':{'state':'unavailable'},
                'coverage':{},'alerts':[{'code':'COLLECTOR_UNAVAILABLE','scope':'collector','severity':'critical'}]}
    view['database'] = results[1] if not isinstance(results[1], BaseException) else {'status':'unavailable'}
    if view['database'].get('status') != 'available':
        view['alerts'].append({'code':'DATABASE_METRICS_UNAVAILABLE','severity':'warning','scope':'database'})
    view['alerts'].extend(database_alerts(view['database']))
    view['coverage']['database_metrics'] = view['database']['status']
    view['storage'] = results[2] if not isinstance(results[2], BaseException) else {'status':'unavailable'}
    if view['storage'].get('status') != 'available':
        view['alerts'].append({'code':'STORAGE_METRICS_UNAVAILABLE','severity':'warning','scope':'storage'})
    view['coverage']['managed_storage'] = view['storage'].get('status','unavailable')
    if production:
        view['owners'] = results[3] if not isinstance(results[3], BaseException) else {'status':'unavailable','rows':None,'page_complete':False}
        if view['owners'].get('status') == 'unavailable':
            view['alerts'].append({'code':'OWNER_METRICS_UNAVAILABLE','severity':'warning','scope':'users',
                                   'message':'每用户运行统计暂时无法取得，不能按零用量处理'})
            view['coverage']['每用户资源与任务'] = '采集不可用；不能按零用量处理'
        else:
            measured = sum(i.get('usage',{}).get('status') == 'available' for o in view['owners'].get('rows') or [] for i in o.get('instances',[]))
            count = sum(len(o.get('instances',[])) for o in view['owners'].get('rows') or [])
            view['coverage']['每用户资源与任务'] = f'运行账本与排队已读取，实例用量已取得 {measured}/{count}；工作目录、临时空间与失败任务统计待接入'
        for owner in view['owners'].get('rows') or []:
            for pool, queue in owner.get('queues', {}).items():
                if queue['waiting'] and (queue['oldest_seconds'] >= QUEUE_WARNING_SECONDS or queue['waiting'] >= 5):
                    view['alerts'].append({'code':'QUEUE_WAIT_HIGH','scope':owner['owner_label']+' / '+pool,
                        'severity':'critical' if queue['oldest_seconds'] >= QUEUE_CRITICAL_SECONDS or queue['waiting'] >= 20 else 'warning',
                        'waiting':queue['waiting'],'oldest_seconds':round(queue['oldest_seconds'])})
    view['queue_thresholds'] = {'warning_seconds':QUEUE_WARNING_SECONDS,'critical_seconds':QUEUE_CRITICAL_SECONDS,
                                'warning_waiting':5,'critical_waiting':20}
    view['generated_at'] = time.time()
    return view


def install(app, environ=None):
    env = os.environ if environ is None else environ
    if env.get('MAIMI_CLUSTER_MONITOR_ENABLED') != 'true':
        return
    allowed = DEVELOPERS
    if 'MAIMI_MONITOR_READ_USERS' in env:
        requested = frozenset(x.strip() for x in env['MAIMI_MONITOR_READ_USERS'].split(',') if x.strip())
        if not requested <= allowed:
            raise ValueError('monitor_authorization_invalid')
        allowed = requested
    router = APIRouter()
    requests = defaultdict(deque)
    snapshot_lock = asyncio.Lock()
    cached_snapshot = b''
    snapshot_expiry = 0.0
    overview_lock = asyncio.Lock()
    cached_overview = {}
    overview_expiry = prune_at = 0.0

    async def gate(request, details=True):
        nonlocal prune_at
        if request.headers.get('sec-fetch-site') == 'cross-site' or request.url.query:
            raise Denied(403, 'MONITOR_ACCESS_DENIED')
        owner = await actual_iam(request)
        if not isinstance(owner, str) or not USER.fullmatch(owner) or (details and owner not in allowed):
            raise Denied(403, 'MONITOR_ACCESS_DENIED')
        now = time.monotonic()
        if now >= prune_at:
            for key in [key for key, queue in requests.items() if not queue or queue[-1] <= now - 60]:
                del requests[key]
            prune_at = now + 60
        q = requests[owner]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= 30:
            raise Denied(429, 'MONITOR_REFRESH_LIMIT')
        q.append(now)
        return owner

    @router.get('/v1/developer-monitor/overview')
    async def overview(request: Request):
        nonlocal cached_overview, overview_expiry
        try:
            owner = await gate(request, details=False)
            async with overview_lock:
                if time.monotonic() >= overview_expiry:
                    cached_overview = await employee_overview()
                    overview_expiry = time.monotonic() + 20
                return JSONResponse({**cached_overview, 'can_view_details': owner in allowed}, headers=HEADERS)
        except Denied as error:
            return JSONResponse({'error': error.code}, status_code=error.status, headers=HEADERS)
        except Exception:
            return JSONResponse({'error': 'MONITOR_DATA_UNAVAILABLE'}, status_code=503, headers=HEADERS)

    @router.get('/v1/developer-monitor/cluster')
    async def snapshot(request: Request):
        nonlocal cached_snapshot, snapshot_expiry
        try:
            await gate(request)
            async with snapshot_lock:
                if time.monotonic() >= snapshot_expiry:
                    value = await report()
                    from ..distribution import current_release
                    release = await asyncio.to_thread(current_release)
                    value['desktop_release'] = release[0] if release else None
                    for section, rows in (('owners', 'rows'), ('storage', 'users')):
                        part = value.get(section) or {}
                        for row in part.get(rows) or []:
                            row.pop('owner_key', None)
                            row.pop('user_no', None)
                        part.pop('next_after_owner', None)
                    cached_snapshot = JSONResponse(value).body
                    snapshot_expiry = time.monotonic() + 5
                return Response(cached_snapshot, media_type='application/json', headers=HEADERS)
        except Denied as error:
            return JSONResponse({'error': error.code}, status_code=error.status, headers=HEADERS)
        except Exception:
            return JSONResponse({'error': 'MONITOR_DATA_UNAVAILABLE'}, status_code=503, headers=HEADERS)

    @router.get('/maimi/monitor/')
    @router.get('/maimi/monitor/assets/{name}')
    async def asset(request: Request, name: str = 'index.html'):
        # The public shell contains no system data. Data API always checks IAM.
        types = {'index.html': 'text/html', 'app.js': 'text/javascript', 'app.css': 'text/css'}
        if name not in types:
            return Response(status_code=404, headers=HEADERS)
        headers = {**HEADERS, 'Content-Security-Policy': "default-src 'none'; img-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"}
        if name == 'index.html':
            return Response(version_links((ASSETS / name).read_text('utf8')), media_type=types[name], headers=headers)
        return asset_response(request, MONITOR + name, types[name], headers)
    @router.get('/v1/developer-monitor/history/{window}')
    async def history(request: Request, window: str):
        try:
            await gate(request)
            from .history_runtime import enabled
            if not enabled():
                return JSONResponse({'status':'not_configured'}, headers=HEADERS)
            if window not in ('day','week','month'):
                return JSONResponse({'error':'INVALID_WINDOW'},status_code=400,headers=HEADERS)
            from .history_store import history as read_history
            return JSONResponse(await asyncio.to_thread(read_history,window),headers=HEADERS)
        except Denied as error:
            return JSONResponse({'error':error.code},status_code=error.status,headers=HEADERS)
        except Exception:
            return JSONResponse({'status':'unavailable'},status_code=503,headers=HEADERS)
    app.include_router(router)
    from .history_runtime import install as install_history
    install_history(app, report)
