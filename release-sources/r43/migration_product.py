"""Bounded card metadata migration. No artifact data or credentials are returned."""
import hashlib
import json

PREVIOUS='016_project_session_state.sql'
PREVIOUS_SHA='6d28110af7a7b53f0626e2e74d3dd6e7a196303f775213e45ccb450c3b5f50e1'
NAME='017_card_print_artifact_limit.sql'
SQL_SHA='dbbbd9221a51f20bca6ce853c348a0bc8315c68b0b9f279e6d49e41c0a28c375'
class Stop(ValueError):pass

def require(value,code):
    if not value:raise Stop(code)

def ledger(cur):
    cur.execute('SELECT version,checksum FROM platform.schema_migrations WHERE version=%s OR version >= %s ORDER BY version',(PREVIOUS,NAME))
    rows=cur.fetchall()
    require(len(rows) in (1,2),'R43_MIGRATION_LEDGER_SCOPE_CHANGED')
    data={row['version']:row['checksum'] for row in rows}
    require(len(data)==len(rows) and set(data) <= {PREVIOUS,NAME} and data.get(PREVIOUS)==PREVIOUS_SHA,'R43_PREDECESSOR_MIGRATION_CHANGED')
    if NAME in data:require(data[NAME]==SQL_SHA,'R43_MIGRATION_CHECKSUM_CHANGED')
    return NAME in data

def inspect(cur,classify,expected):
    applied=ledger(cur)
    catalog=classify(cur,expected)
    require(catalog['mode']==('MIGRATED' if applied else 'ORIGINAL'),'R43_MIGRATION_SCHEMA_LEDGER_MISMATCH')
    return {'schema':'card-storage-r43-database-v1','status':'VERIFIED','mode':catalog['mode'],
        'migration':NAME,'checksum':SQL_SHA,'predecessor_checksum':PREVIOUS_SHA,
        'artifact_schema_sha256':catalog['schema_sha256'],'ordinary_limit_bytes':33554432,
        'card_print_limit_bytes':134217728 if applied else 33554432,
        'business_values_read':False,'business_rows_modified':False}

def execute(connection,classify,expected,sql,*,apply=False):
    require(isinstance(sql,bytes) and hashlib.sha256(sql).hexdigest()==SQL_SHA,'R43_MIGRATION_PAYLOAD_CHANGED')
    # The caller supplies an autocommit connection so this block owns the full
    # transaction. Nested savepoints must not be mistaken for a durable commit.
    require(connection.autocommit is True,'R43_CONNECTION_REQUIRES_AUTOCOMMIT')
    with connection.transaction():
        with connection.cursor() as cur:
            # Apply must see admissions committed while it waited for their
            # advisory lock; a pre-lock repeatable-read snapshot would be stale.
            cur.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED' if apply else 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            cur.execute("SET LOCAL lock_timeout='5s'")
            cur.execute("SET LOCAL statement_timeout='45s'")
            cur.execute("SET LOCAL idle_in_transaction_session_timeout='15s'")
            cur.execute('SET LOCAL search_path=pg_catalog')
            if apply:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('megmeet.schema_migrations'))")
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended('maimi-project-admission',0))")
                cur.execute("SELECT count(*) AS active FROM platform.project_jobs WHERE status IN ('running','cancelling')")
                require(cur.fetchone()['active']==0,'R43_ACTIVE_PROJECT_TASKS_WAIT_BEFORE_MAINTENANCE')
            before=inspect(cur,classify,expected)
            if apply and before['mode']=='ORIGINAL':
                cur.execute(sql.decode('utf-8'))
                cur.execute('INSERT INTO platform.schema_migrations(version,checksum) VALUES(%s,%s)',(NAME,SQL_SHA))
                result=inspect(cur,classify,expected)
                require(result['mode']=='MIGRATED','R43_MIGRATION_NOT_VERIFIED')
                result.update(status='APPLIED',transaction_effect='SCHEMA_AND_LEDGER_ATOMIC')
            else:
                result=dict(before)
                if apply:result.update(status='ALREADY_APPLIED',transaction_effect='NO_DDL')
    # Only returned after the context manager actually committed, or finished
    # its read-only transaction. A disconnected commit is reconciled by inspect.
    return result

def remote_code(module_raw,helper_raw,expected,sql,apply,preserved_raw,preserved_expected):
    """Only public frozen code/catalog/DDL go in argv; DATABASE_URL stays inside."""
    require(type(apply) is bool,'R43_ACTION_INVALID')
    return '''import os,json
import psycopg
from psycopg.rows import dict_row
namespace={'__file__':'/unwritten-r43/artifact_schema.py'}
control={}
preserved={}
try:
 exec(compile(%r,'artifact_schema.py','exec'),namespace)
 exec(compile(%r,'migration_product.py','exec'),control)
 exec(compile(%r,'project_schema.py','exec'),preserved)
 def classify(cur,expected):
  preserved['verify'](cur,%r)
  return namespace['classify_artifact_schema'](cur,expected)
 with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,connect_timeout=5,row_factory=dict_row) as connection:
  result=control['execute'](connection,classify,%r,%r,apply=%r)
 result['preserved_project_tables_verified']=True
 print(json.dumps(result,separators=(',',':')))
except Exception as exc:
 fixed={'R43_MIGRATION_LEDGER_SCOPE_CHANGED','R43_PREDECESSOR_MIGRATION_CHANGED','R43_MIGRATION_CHECKSUM_CHANGED','R43_MIGRATION_SCHEMA_LEDGER_MISMATCH','R43_MIGRATION_PAYLOAD_CHANGED','R43_CONNECTION_REQUIRES_AUTOCOMMIT','R43_ACTIVE_PROJECT_TASKS_WAIT_BEFORE_MAINTENANCE','R43_MIGRATION_NOT_VERIFIED','ARTIFACT_TABLE_ABSENT','ARTIFACT_SCHEMA_MISMATCH','R43_PRESERVED_SCHEMA_EXPECTATION_INVALID','R43_PRESERVED_PROJECT_SCHEMA_CHANGED'}
 code=str(exc) if type(exc).__name__ in {'Stop','RuntimeError'} and str(exc) in fixed else {'LockNotAvailable':'R43_DATABASE_BUSY','QueryCanceled':'R43_DATABASE_TIMEOUT','OperationalError':'R43_DATABASE_UNAVAILABLE','SerializationFailure':'R43_DATABASE_CONCURRENT_CHANGE'}.get(type(exc).__name__,'R43_DATABASE_CHECK_FAILED')
 print(json.dumps({'schema':'card-storage-r43-database-v1','status':'STOP','reason':code,'credentials_or_business_values_output':False},separators=(',',':')))
''' % (helper_raw.decode('utf-8'),module_raw.decode('utf-8'),preserved_raw.decode('utf-8'),preserved_expected,expected,sql,apply)
