"""Administrator-only runtime metadata. No prompts, events, commands or file names.

CPU/memory are measured per immutable Pod UID. Reservation budgets are separate.
Workspace/tmp disk accounting needs a trusted per-owner collector and stays unknown.
"""
import concurrent.futures, datetime, decimal, ipaddress, json, math, os, re, ssl, time
from . import production_monitor as monitor

NAMESPACES = ('maimi-production', 'maimi-production-execution')
PREFIX = 'maimi.megmeet.com/'
OWNER = re.compile(r'[0-9a-f]{32}')
MAX_OWNERS = 500

def epoch(value):
    try:
        parsed = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None

def quantity(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)([numkKMGTPE]i?)?', str(value))
    if not match:
        raise ValueError('quantity')
    units = {'':1, 'n':1e-9, 'u':1e-6, 'm':.001, 'k':1000, 'K':1000}
    units.update({k:1000**i for i,k in enumerate('MGTPE',2)})
    units.update({k+'i':1024**i for i,k in enumerate('KMGTPE',1)})
    number = float(match[1])*units[match[2] or '']
    if not math.isfinite(number) or number < 0:
        raise ValueError('quantity')
    return number

def api_samples():
    """Only metadata (no Pod spec/env) and usage; three bounded calls per namespace."""
    address = os.environ.get('KUBERNETES_SERVICE_HOST', '')
    if ipaddress.ip_address(address) not in ipaddress.ip_network(os.environ.get('MAIMI_MONITOR_SERVICE_CIDR', '')):
        raise ValueError('production_api')
    ca = monitor.read_projected('ca.crt', 32768)
    import hashlib
    if hashlib.sha256(ca).hexdigest() != monitor.CA_SHA:
        raise ValueError('production_ca')
    context = ssl.create_default_context(cadata=ca.decode('ascii'))
    token = monitor.read_projected('token',16384).decode('ascii').strip()
    if not token or '\n' in token or '\r' in token:
        raise ValueError('token_format')

    def get(namespace, metrics=False):
        connection = monitor.APIConnection(address, context)
        base = '/apis/metrics.k8s.io/v1beta1' if metrics else '/api/v1'
        accept = 'application/json' if metrics else 'application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1'
        try:
            connection.request('GET', base+'/namespaces/'+namespace+'/pods'+('' if metrics else '?limit=500'),
                               headers={'Authorization':'Bearer '+token,'Accept':accept,'Connection':'close'})
            response=connection.getresponse();raw=response.read(1048577)
            if response.status!=200 or len(raw)>1048576:
                raise ValueError('api_response')
            value=json.loads(raw)
            if not metrics and value.get('kind')!='PartialObjectMetadataList':
                raise ValueError('metadata_only_required')
            if value.get('metadata',{}).get('continue'):
                raise ValueError('metadata_truncated')
            return value
        finally:
            connection.close()

    def namespace_samples(namespace):
        try:
            before=get(namespace); measured=get(namespace,True); after=get(namespace)
            return {'status':'available','before':before,'metrics':measured,'after':after}
        except Exception:
            return {'status':'unavailable'}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        return dict(zip(NAMESPACES,executor.map(namespace_samples,NAMESPACES)))

def ledger_view():
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=3,row_factory=dict_row) as connection,connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("SET LOCAL statement_timeout='3s'")
        cursor.execute("SET LOCAL lock_timeout='500ms'")
        # Active owners include queue-only users. Bounded metadata, never handle secrets.
        cursor.execute('''SELECT owner_key FROM (
          SELECT owner_key FROM platform.worker_runtime_reservations WHERE state<>'RELEASED'
          UNION SELECT owner_key FROM execution.worker_runtime_reservations WHERE state<>'RELEASED'
          UNION SELECT owner_key FROM platform.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()
          UNION SELECT owner_key FROM execution.worker_runtime_queue WHERE state='WAITING' AND expires_at>clock_timestamp()
        ) active ORDER BY owner_key LIMIT %s''',(MAX_OWNERS+1,))
        keys=[row['owner_key'] for row in cursor.fetchall()];complete=len(keys)<=MAX_OWNERS;keys=keys[:MAX_OWNERS]
        if not all(OWNER.fullmatch(key) for key in keys):
            raise ValueError('owner_identity')
        owners={key:{'owner_key':key,'owner_label':'用户 …'+key[-6:],'instances':[],'queues':{},
                     'workspace_bytes':None,'temporary_bytes':None,'failed_tasks':None} for key in keys}
        for pool,schema in (('chat','platform'),('execution','execution')):
            cursor.execute(f'''SELECT owner_key,generation,state,cpu_millis,memory_mib,ephemeral_mib,
              execution_node,resource_class,attention_required,
              handle->>'namespace' AS namespace,handle->>'name' AS pod_name,handle->>'pod_uid' AS pod_uid,
              handle->>'owner' AS handle_owner,handle->>'generation' AS handle_generation,
              handle->>'instance' AS instance
              FROM {schema}.worker_runtime_reservations
              WHERE state<>'RELEASED' AND owner_key=ANY(%s) ORDER BY owner_key,generation''',(keys,))
            for row in cursor.fetchall():
                owners[row['owner_key']]['instances'].append({**row,'pool':pool})
            cursor.execute(f'''SELECT owner_key,count(*) AS waiting,
              EXTRACT(epoch FROM clock_timestamp()-min(created_at)) AS oldest_seconds
              FROM {schema}.worker_runtime_queue
              WHERE state='WAITING' AND expires_at>clock_timestamp() AND owner_key=ANY(%s) GROUP BY owner_key''',(keys,))
            queues={row['owner_key']:{'waiting':row['waiting'],'oldest_seconds':float(row['oldest_seconds'])} for row in cursor.fetchall()}
            for key,row in owners.items():
                row['queues'][pool]=queues.get(key,{'waiting':0,'oldest_seconds':0})
        return {'rows':list(owners.values()),'page_complete':complete}

def bind_usage(instance,samples,now):
    unknown={'status':'unavailable','cpu_cores':None,'memory_bytes':None}
    namespace=instance.get('namespace')
    if namespace not in NAMESPACES:
        return {**unknown,'reason':'NO_BOUND_RUNTIME'}
    source=samples.get(namespace,{})
    if source.get('status')!='available':
        return {**unknown,'reason':'USAGE_COLLECTION_FAILED'}
    name=instance.get('pod_name');owner=instance['owner_key'];generation=str(instance['generation'])
    if instance.get('handle_owner')!=owner or str(instance.get('handle_generation'))!=generation:
        return {**unknown,'reason':'HANDLE_OWNER_MISMATCH'}
    def select(kind):
        return [item for item in source[kind].get('items',[]) if item.get('metadata',{}).get('name')==name]
    before,after,measured=select('before'),select('after'),select('metrics')
    if len(before)!=1 or len(after)!=1 or len(measured)!=1:
        return {**unknown,'reason':'POD_OR_METRIC_MISSING'}
    for pod in (before[0],after[0]):
        meta=pod['metadata'];labels=meta.get('labels',{})
        if (meta.get('uid')!=instance.get('pod_uid') or meta.get('namespace')!=namespace or
            labels.get(PREFIX+'owner')!=owner or labels.get(PREFIX+'generation')!=generation or
            labels.get(PREFIX+'instance')!=instance.get('instance') or meta.get('deletionTimestamp')):
            return {**unknown,'reason':'POD_IDENTITY_CHANGED'}
    metric=measured[0];at=epoch(metric.get('timestamp'));created=epoch(after[0]['metadata'].get('creationTimestamp'))
    if metric.get('metadata',{}).get('namespace')!=namespace or at is None or created is None or not created<=at<=now+5 or now-at>90:
        return {**unknown,'reason':'USAGE_STALE_OR_PREVIOUS_GENERATION'}
    try:
        containers=metric['containers']
        if not containers or len({c['name'] for c in containers})!=len(containers):
            raise ValueError('containers')
        cpu=sum(quantity(c['usage']['cpu']) for c in containers)
        memory=sum(quantity(c['usage']['memory']) for c in containers)
    except (KeyError,TypeError,ValueError):
        return {**unknown,'reason':'USAGE_FIELDS_MISSING'}
    return {'status':'available','cpu_cores':cpu,'memory_bytes':memory,'sampled_at':at,'container_count':len(containers)}

def adapt(ledger,samples,now=None):
    now=time.time() if now is None else now;rows=[]
    for source in ledger['rows']:
        row={k:v for k,v in source.items() if k!='instances'};row['instances']=[]
        for instance in source['instances']:
            usage=bind_usage(instance,samples,now)
            row['instances'].append({k:instance.get(k) for k in ('pool','generation','state','cpu_millis','memory_mib','ephemeral_mib','execution_node','resource_class','attention_required')})
            row['instances'][-1]['usage']=usage
        rows.append(row)
    return {'status':'partial','rows':rows,'page_complete':ledger['page_complete'],'maximum_owners':MAX_OWNERS,
            'scope':'active_runtime_and_queue_owners','employee_identity_included':False,
            'missing':['workspace_bytes','temporary_bytes','failed_tasks'],
            'note':'只读管理账本与绑定实例用量；工作目录、临时文件及任务失败统计尚未接入，未知不按零计算。'}

def collect():
    ledger=ledger_view()
    try:
        samples=api_samples() if ledger['rows'] else {}
    except Exception:
        samples={}
    return adapt(ledger,samples)
