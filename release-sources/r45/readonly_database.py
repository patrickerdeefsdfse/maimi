"""R45 has no DDL or mutation branch; only exact R43 database metadata proof."""
NAME='017_card_print_artifact_limit.sql'
SQL_SHA='dbbbd9221a51f20bca6ce853c348a0bc8315c68b0b9f279e6d49e41c0a28c375'
PREVIOUS='016_project_session_state.sql'
PREVIOUS_SHA='6d28110af7a7b53f0626e2e74d3dd6e7a196303f775213e45ccb450c3b5f50e1'
class Stop(ValueError):pass
def require(v,c):
 if not v:raise Stop(c)
def execute(connection,classify,expected,preserve,preserved_expected):
 require(connection.autocommit is True,'R45_DATABASE_CONNECTION_INVALID')
 with connection.transaction():
  with connection.cursor() as cur:
   cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
   cur.execute("SET LOCAL lock_timeout='5s'")
   cur.execute("SET LOCAL statement_timeout='45s'")
   cur.execute("SET LOCAL idle_in_transaction_session_timeout='15s'")
   cur.execute('SET LOCAL search_path=pg_catalog')
   cur.execute('SELECT version,checksum FROM platform.schema_migrations WHERE version=%s OR version >= %s ORDER BY version',(PREVIOUS,NAME))
   rows=cur.fetchall()
   require(len(rows)==2 and {row['version']:row['checksum'] for row in rows}=={PREVIOUS:PREVIOUS_SHA,NAME:SQL_SHA},'R45_DATABASE_LEDGER_CHANGED')
   preserve(cur,preserved_expected);catalog=classify(cur,expected)
   require(catalog['mode']=='MIGRATED','R45_DATABASE_017_NOT_MIGRATED')
 return {'schema':'logistics-r45-database-v1','status':'VERIFIED','mode':'MIGRATED','migration':NAME,'checksum':SQL_SHA,'artifact_schema_sha256':catalog['schema_sha256'],'preserved_project_tables_verified':True,'ordinary_limit_bytes':33554432,'card_print_limit_bytes':134217728,'transaction':'READ_ONLY','business_values_read':False,'database_changed':False}
def remote_code(module,helper,expected,preserved,preserved_expected):
 return '''import os,json
import psycopg
from psycopg.rows import dict_row
helper={'__file__':'/unwritten-r45/artifact_schema.py'};control={};preserved={}
try:
 exec(compile(%r,'artifact_schema.py','exec'),helper)
 exec(compile(%r,'readonly_database.py','exec'),control)
 exec(compile(%r,'project_schema.py','exec'),preserved)
 with psycopg.connect(os.environ['DATABASE_URL'],autocommit=True,connect_timeout=5,row_factory=dict_row) as connection:
  result=control['execute'](connection,helper['classify_artifact_schema'],%r,preserved['verify'],%r)
 print(json.dumps(result,separators=(',',':')))
except Exception as exc:
 fixed={'R45_DATABASE_CONNECTION_INVALID','R45_DATABASE_LEDGER_CHANGED','R45_DATABASE_017_NOT_MIGRATED','ARTIFACT_TABLE_ABSENT','ARTIFACT_SCHEMA_MISMATCH','R43_PRESERVED_SCHEMA_EXPECTATION_INVALID','R43_PRESERVED_PROJECT_SCHEMA_CHANGED'}
 code=str(exc) if type(exc).__name__ in {'Stop','RuntimeError'} and str(exc) in fixed else {'LockNotAvailable':'R45_DATABASE_BUSY','QueryCanceled':'R45_DATABASE_TIMEOUT','OperationalError':'R45_DATABASE_UNAVAILABLE'}.get(type(exc).__name__,'R45_DATABASE_CHECK_FAILED')
 print(json.dumps({'schema':'logistics-r45-database-v1','status':'STOP','reason':code,'credentials_or_business_values_output':False},separators=(',',':')))
''' % (helper.decode('utf-8'),module.decode('utf-8'),preserved.decode('utf-8'),expected,preserved_expected)
