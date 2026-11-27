"""Bounded PostgreSQL monitoring. Only allowlisted numeric operational metadata.

Minutes: 3 days; hours: 30 days; daily aggregate: 180 days. Per-user daily
metadata: 30 days. No prompts, filenames, URLs, credentials or raw logs.
"""
import hashlib, math, os, re, time
import psycopg
from psycopg.rows import dict_row

DAY=86400
RETENTION={'minutes_days':3,'hours_days':30,'daily_days':180,'per_user_days':30,'device_days':30}
def day(at): return (int(at)+28800)//DAY*DAY-28800
def owner_key(user):
    if not isinstance(user,str) or not re.fullmatch(r'M\d{8}',user): raise ValueError('OWNER_INVALID')
    from uuid import uuid5, NAMESPACE_URL
    return uuid5(NAMESPACE_URL,'megmeet:personal-cloud:v1:'+user).hex
def number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
def connect():
    c=psycopg.connect(os.environ['DATABASE_URL'],connect_timeout=3,row_factory=dict_row)
    c.execute("SET statement_timeout='5s'");c.execute("SET lock_timeout='500ms'")
    return c
def flatten(view):
    """Unknown, stale and missing observations never become zero."""
    out=[]
    def add(metric,scope,value):
        if number(value): out.append((metric,scope,float(value)))
    for role,h in view.get('hosts',{}).items():
        if role not in ('A1','A2','B1','B2','C') or h.get('status')!='observed':continue
        r=h.get('resources',{});m=r.get('memory',{})
        add('host_cpu',role,r.get('cpu_used_ratio'))
        if number(m.get('MemTotal')) and m['MemTotal']>0 and number(m.get('MemAvailable')):
            add('host_memory',role,max(0,1-m['MemAvailable']/m['MemTotal']))
        for disk in ('system','data'):
            d=r.get('disks',{}).get(disk,{})
            if number(d.get('total_bytes')) and d['total_bytes']>0 and number(d.get('available_bytes')):
                add('disk_'+disk,role,max(0,1-d['available_bytes']/d['total_bytes']))
    db=view.get('database',{})
    if db.get('status')=='available':
        add('db_connections','all',db.get('connections'))
        for pool in ('chat','execution'):
            p=db.get('pools',{}).get(pool,{})
            if isinstance(p.get('states'),dict):add('allocated',pool,sum(p['states'].values()))
            add('waiting',pool,p.get('waiting'));add('wait_seconds',pool,p.get('oldest_seconds'))
    s=view.get('storage',{})
    if s.get('status')=='available':add('storage_bytes','all',s.get('global',{}).get('charged_bytes'))
    return out

def save_sample(view,now=None):
    now=int(time.time() if now is None else now);bucket=now//60*60
    with connect() as c:
        if not c.execute('SELECT pg_try_advisory_xact_lock(1010101) AS ok').fetchone()['ok']:return False
        c.execute("INSERT INTO maimi_monitor.state VALUES ('last_sample',%s) ON CONFLICT(name) DO NOTHING",(bucket,))
        # All APs use the same minute key; missing fields from one AP may be filled by the other.
        for metric,scope,value in flatten(view):
            c.execute('INSERT INTO maimi_monitor.series VALUES (60,%s,%s,%s,1,%s,%s,%s) ON CONFLICT DO NOTHING',
                      (bucket,metric,scope,value,value,value))
        c.execute("UPDATE maimi_monitor.state SET value=greatest(value,%s) WHERE name='last_sample'",(bucket,))
        c.execute("INSERT INTO maimi_monitor.state VALUES ('last_maintenance',0) ON CONFLICT DO NOTHING")
        last=c.execute("SELECT value FROM maimi_monitor.state WHERE name='last_maintenance'").fetchone()['value']
        if now-last>=300:
            maintenance(c,now)
            c.execute("UPDATE maimi_monitor.state SET value=%s WHERE name='last_maintenance'",(now,))
    return True

def maintenance(c,now):
    # Recompute retained fine intervals before deletion; UPSERT is idempotent,
    # includes late observations, and never sums daily distinct counts into WAU.
    for source,target,offset in ((60,3600,0),(3600,DAY,28800)):
        boundary=(now+offset)//target*target-offset
        c.execute('''INSERT INTO maimi_monitor.series(step,bucket,metric,scope,samples,total,low,high)
          SELECT %s,((bucket+%s)/%s)*%s-%s,metric,scope,sum(samples),sum(total),min(low),max(high)
          FROM maimi_monitor.series WHERE step=%s AND bucket<%s
          GROUP BY 2,metric,scope ON CONFLICT(step,bucket,metric,scope)
          DO UPDATE SET samples=excluded.samples,total=excluded.total,low=excluded.low,high=excluded.high
          WHERE excluded.samples>=maimi_monitor.series.samples''',
          (target,offset,target,target,offset,source,boundary))
    # Keep exact user-day sets for 30 days, aggregate distinct totals for 180.
    started=c.execute("SELECT value FROM maimi_monitor.state WHERE name='started_at'").fetchone()['value']
    for d in range(max(day(started),day(now)-7*DAY),day(now)+1,DAY):
        c.execute('''INSERT INTO maimi_monitor.day_totals
          SELECT %s,count(DISTINCT owner_key) FILTER(WHERE day=%s),count(DISTINCT owner_key),
          coalesce(sum(requests) FILTER(WHERE day=%s AND kind='model'),0),
          coalesce(sum(failed) FILTER(WHERE day=%s),0),
          coalesce(sum(input_tokens) FILTER(WHERE day=%s),0),coalesce(sum(output_tokens) FILTER(WHERE day=%s),0),
          coalesce(sum(token_samples) FILTER(WHERE day=%s),0),%s
          FROM maimi_monitor.user_days WHERE day BETWEEN %s AND %s
          ON CONFLICT(day) DO UPDATE SET dau=excluded.dau,wau=excluded.wau,model_requests=excluded.model_requests,
          failed=excluded.failed,input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
          token_samples=excluded.token_samples,updated_at=excluded.updated_at''',
          (d,d,d,d,d,d,d,now,d-6*DAY,d))
    for step,days in ((60,RETENTION['minutes_days']),(3600,RETENTION['hours_days']),(DAY,RETENTION['daily_days'])):
        # Delete complete buckets only, so subsequent rollups never overwrite a
        # complete coarse bucket with a truncated remainder of finer samples.
        boundary=(now//DAY-days)*DAY
        c.execute('DELETE FROM maimi_monitor.series WHERE step=%s AND bucket<%s',(step,boundary))
    for table,column,cutoff in (('receipts','recorded_at',now-DAY),('user_days','day',day(now)-RETENTION['per_user_days']*DAY),
                                ('day_totals','day',day(now)-RETENTION['daily_days']*DAY),('devices','last_seen',now-RETENTION['device_days']*DAY)):
        c.execute(f'DELETE FROM maimi_monitor.{table} WHERE {column}<%s',(cutoff,))

class EventStore:
    def enable_source(self,kind):pass
    def record_loss(self,kind,stamp):
        with connect() as c:
            c.execute("INSERT INTO maimi_monitor.state VALUES ('last_event_loss',%s) ON CONFLICT(name) DO UPDATE SET value=greatest(maimi_monitor.state.value,excluded.value)",(int(stamp),))
    def record_event(self,*,trusted_owner,event):
        from uuid import UUID
        key=owner_key(trusted_owner);uid=str(UUID(event['event_id']));now=int(time.time());at=event['completed_at']
        if not number(at) or not now-300<=at<=now+5:raise ValueError('EVENT_TIME')
        kind=event['kind'];status=event['status'];source=event.get('source','combined')
        if kind not in ('model','task','office','activity') or status not in ('succeeded','failed','cancelled','unknown') or source not in ('web','desktop','combined'):raise ValueError('EVENT_SCHEMA')
        def count(name):
            n=event.get(name);return int(n) if type(n) is int and 0<=n<=10**12 else 0
        with connect() as c:
            fresh=c.execute('INSERT INTO maimi_monitor.receipts VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING id',(uid,now)).fetchone()
            if not fresh:return
            values=(day(at),key,source,kind,1,int(status=='failed'),int(status=='unknown'),count('duration_ms'),
                    count('input_tokens'),count('output_tokens'),int(type(event.get('input_tokens')) is int and type(event.get('output_tokens')) is int),int(at))
            c.execute('''INSERT INTO maimi_monitor.user_days VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(day,owner_key,source,kind) DO UPDATE SET requests=maimi_monitor.user_days.requests+1,
              failed=maimi_monitor.user_days.failed+excluded.failed,unknown=maimi_monitor.user_days.unknown+excluded.unknown,
              duration_ms=maimi_monitor.user_days.duration_ms+excluded.duration_ms,
              input_tokens=maimi_monitor.user_days.input_tokens+excluded.input_tokens,output_tokens=maimi_monitor.user_days.output_tokens+excluded.output_tokens,
              token_samples=maimi_monitor.user_days.token_samples+excluded.token_samples,last_seen=greatest(maimi_monitor.user_days.last_seen,excluded.last_seen)''',values)
    record_observed_event=record_event

def device_seen(user,instance,version):
    from uuid import UUID
    key=owner_key(user);instance=str(UUID(instance));now=int(time.time())
    if not re.fullmatch(r'[A-Za-z0-9.+_-]{1,64}',version):raise ValueError('DEVICE_VERSION')
    device=hashlib.sha256((key+':'+instance).encode()).hexdigest()[:32]
    with connect() as c:
        c.execute('''INSERT INTO maimi_monitor.devices VALUES (%s,%s,%s,%s) ON CONFLICT(owner_key,device_key)
          DO UPDATE SET version=excluded.version,last_seen=excluded.last_seen
          WHERE maimi_monitor.devices.last_seen<%s''',(key,device,version,now,now-50))

def history(window):
    if window not in ('day','week','month'):raise ValueError('WINDOW')
    now=int(time.time());days={'day':1,'week':7,'month':30}[window];step=60 if window=='day' else 3600
    since=now-days*DAY
    with connect() as c:
        c.execute('SET TRANSACTION READ ONLY')
        states={r['name']:r['value'] for r in c.execute('SELECT name,value FROM maimi_monitor.state')}
        database_bytes=c.execute("SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint AS bytes FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='maimi_monitor' AND c.relkind='r'").fetchone()['bytes']
        points=list(c.execute('''SELECT bucket,metric,scope,samples,total/samples AS average,high FROM maimi_monitor.series
          WHERE step=%s AND bucket>=%s ORDER BY bucket,metric,scope LIMIT 50000''',(step,since)))
        calendar_start = day(now) - (days - 1) * DAY
        totals=list(c.execute('SELECT * FROM maimi_monitor.day_totals WHERE day BETWEEN %s AND %s ORDER BY day',
                              (calendar_start,day(now))))
        channels=list(c.execute('''SELECT day,
            count(DISTINCT owner_key) FILTER(WHERE source='web') AS web,
            count(DISTINCT owner_key) FILTER(WHERE source='desktop') AS desktop
            FROM maimi_monitor.user_days
            WHERE day BETWEEN %s AND %s AND source IN ('web','desktop')
            GROUP BY day ORDER BY day''',(calendar_start,day(now))))
        active=c.execute('SELECT count(DISTINCT owner_key) FILTER(WHERE day=%s) AS dau,count(DISTINCT owner_key) AS wau FROM maimi_monitor.user_days WHERE day BETWEEN %s AND %s',(day(now),day(now)-6*DAY,day(now))).fetchone()
        users=list(c.execute('''SELECT owner_key,sum(requests) FILTER(WHERE kind='model') AS model_requests,
          sum(failed) AS failed,sum(input_tokens) AS input_tokens,sum(output_tokens) AS output_tokens,
          sum(token_samples) AS token_samples,count(DISTINCT day) AS active_days,max(last_seen) AS last_seen
          FROM maimi_monitor.user_days WHERE day>=%s GROUP BY owner_key
          ORDER BY coalesce(sum(requests) FILTER(WHERE kind='model'),0) DESC,owner_key LIMIT 501''',(calendar_start,)))
        devices=list(c.execute('''SELECT version,count(*) AS devices,count(*) FILTER(WHERE last_seen>=%s) AS recently_online,
          max(last_seen) AS last_seen FROM maimi_monitor.devices GROUP BY version ORDER BY count(*) DESC,version LIMIT 100''',(now-180,)))
    for row in users:
        row['owner_label']='用户 …'+row.pop('owner_key')[-6:]
        for k,v in row.items():
            if k not in ('owner_key','owner_label') and v is not None:row[k]=int(v)
    return {'status':'available','window':window,'step':step,'generated_at':now,'started_at':states.get('started_at'),
      'last_sample':states.get('last_sample'),'last_event_loss':states.get('last_event_loss'),
      'retention':dict(RETENTION),'database_bytes':database_bytes,
      'series':points,'days':totals,'channels':channels,'activity':active,'users':users[:500],'users_complete':len(users)<=500,'devices':devices,
      'scope':'authenticated_model_task_office_and_interactive_requests','desktop_status':'requires_instrumented_company_desktop',
      'note':'日活是实际操作用户去重，周活按七日用户去重；后台心跳不计活跃。离线桌面本地任务不可观测。'}
