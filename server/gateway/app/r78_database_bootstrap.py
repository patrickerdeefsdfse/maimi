"""Initialize only the new r78 test database with separate migration/app roles."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import psycopg
from psycopg import sql

DB='maimi_r78'
ADMIN='maimi_r78_admin'
MIGRATOR='maimi_r78_migrate'
APP='maimi_r78_app'
MIGRATION_ROOT=Path('/app/migrations')
class Refused(RuntimeError):pass
def require(value,reason):
    if not value:raise Refused(reason)
def config():
    installation=os.environ.get('MAIMI_INSTALLATION_ID','')
    require(re.fullmatch('[a-f0-9]{32}',installation),'INSTALLATION_ID_REQUIRED')
    passwords={MIGRATOR:os.environ.get('DATABASE_MIGRATOR_PASSWORD',''),APP:os.environ.get('DATABASE_APP_PASSWORD','')}
    require(all(re.fullmatch('[a-f0-9]{48,128}',v) for v in passwords.values()),'GENERATED_PASSWORDS_REQUIRED')
    require(passwords[APP]!=passwords[MIGRATOR],'ROLE_PASSWORDS_MUST_DIFFER')
    return installation,passwords
def role_check(conn,role):
    row=conn.execute('SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=%s',(role,)).fetchone()
    require(row==(True,False,False,False,False,False),'ROLE_PRIVILEGES_CHANGED')
    require(conn.execute('SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)',(role,)).fetchone()[0]==0,'ROLE_MEMBERSHIP_CHANGED')
def marker(conn,installation):
    require(conn.execute("SELECT to_regclass('maimi_bootstrap.installation')").fetchone()[0] is not None,'BOOTSTRAP_MARKER_REQUIRED')
    require(conn.execute('SELECT installation_id FROM maimi_bootstrap.installation').fetchall()==[(installation,)],'DATABASE_INSTALLATION_CHANGED')
    for role in (MIGRATOR,APP):role_check(conn,role)
    require(conn.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',(DB,)).fetchone()==(MIGRATOR,),'DATABASE_OWNER_CHANGED')
def empty_database(conn):
    """Refuse adoption of an existing application's database before any writes."""
    require(conn.execute('SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=%s',(DB,)).fetchone()==(ADMIN,),'INITIAL_DATABASE_OWNER_CHANGED')
    require(conn.execute("SELECT count(*) FROM pg_namespace WHERE nspname <> 'public' AND nspname <> 'information_schema' AND nspname !~ '^pg_'").fetchone()[0]==0,'NONEMPTY_APPLICATION_DATABASE')
    require(conn.execute('SELECT extname FROM pg_extension ORDER BY extname').fetchall()==[('plpgsql',)],'NONDEFAULT_DATABASE_EXTENSIONS')
    # Relations include tables, sequences, views, indexes and foreign tables.
    # The other catalogs cover schema objects which need not have any relation.
    catalogs=(('pg_class','relnamespace'),('pg_proc','pronamespace'),
              ('pg_type','typnamespace'),('pg_operator','oprnamespace'),
              ('pg_opclass','opcnamespace'),('pg_opfamily','opfnamespace'),
              ('pg_collation','collnamespace'),('pg_conversion','connamespace'),
              ('pg_ts_config','cfgnamespace'),('pg_ts_dict','dictnamespace'),
              ('pg_ts_parser','prsnamespace'),('pg_ts_template','tmplnamespace'),
              ('pg_statistic_ext','stxnamespace'))
    for catalog,column in catalogs:
        count=conn.execute(sql.SQL("SELECT count(*) FROM pg_catalog.{} WHERE {} = 'public'::regnamespace").format(sql.Identifier(catalog),sql.Identifier(column))).fetchone()[0]
        require(count==0,'NONEMPTY_PUBLIC_SCHEMA')
    for catalog in ('pg_largeobject_metadata','pg_event_trigger','pg_publication',
                    'pg_subscription','pg_foreign_server','pg_foreign_data_wrapper',
                    'pg_default_acl'):
        require(conn.execute(sql.SQL('SELECT count(*) FROM pg_catalog.{}').format(sql.Identifier(catalog))).fetchone()[0]==0,'NONEMPTY_DATABASE_OBJECTS')
def migration_ledger(conn):
    files=sorted(MIGRATION_ROOT.glob('*.sql'))
    require(len(files)==28 and files[-1].name=='027_execution_pool.sql' and all(p.is_file() and not p.is_symlink() for p in files),'LOCAL_MIGRATION_SET_REQUIRED')
    # Match app.migrate's UTF-8 text/newline treatment exactly.
    expected=[(p.name,hashlib.sha256(p.read_text(encoding='utf-8').encode('utf-8')).hexdigest()) for p in files]
    require(conn.execute("SELECT to_regclass('platform.schema_migrations')").fetchone()[0] is not None,'MIGRATION_027_REQUIRED')
    actual=conn.execute('SELECT version,checksum FROM platform.schema_migrations ORDER BY version COLLATE "C"').fetchall()
    require(actual==expected,'MIGRATION_LEDGER_MISMATCH')
def run(command):
    installation,passwords=config()
    with psycopg.connect(os.environ['DATABASE_ADMIN_URL'],connect_timeout=10) as conn:
        require(conn.execute('SELECT current_database(),current_user').fetchone()==(DB,ADMIN),'INDEPENDENT_TEST_DATABASE_REQUIRED')
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('maimi.r78.database-bootstrap'))")
        if command=='create-roles':
            present=conn.execute("SELECT to_regclass('maimi_bootstrap.installation')").fetchone()[0]
            if present is None:
                require(conn.execute('SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)',([MIGRATOR,APP],)).fetchone()[0]==0,'EXISTING_ROLES_WITHOUT_MARKER')
                empty_database(conn)
                for role,password in passwords.items():
                    conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
                conn.execute('CREATE SCHEMA maimi_bootstrap')
                conn.execute('REVOKE ALL ON SCHEMA maimi_bootstrap FROM PUBLIC')
                conn.execute('CREATE TABLE maimi_bootstrap.installation(installation_id text PRIMARY KEY)')
                conn.execute('INSERT INTO maimi_bootstrap.installation VALUES (%s)',(installation,))
                conn.execute(sql.SQL('ALTER DATABASE {} OWNER TO {}').format(sql.Identifier(DB),sql.Identifier(MIGRATOR)))
                conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DB)))
                conn.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}, {}').format(sql.Identifier(DB),sql.Identifier(MIGRATOR),sql.Identifier(APP)))
                conn.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            marker(conn,installation)
        elif command=='grant-application':
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('megmeet.schema_migrations'))")
            marker(conn,installation)
            migration_ledger(conn)
            require(conn.execute("SELECT count(*) FROM pg_extension WHERE extname='pgcrypto'").fetchone()[0]==1,'PGCRYPTO_REQUIRED')
            for schema in ('platform','execution','public'):
                conn.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(APP)))
                conn.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(APP)))
                conn.execute(sql.SQL('GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(APP)))
                conn.execute(sql.SQL('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA {} TO {}').format(sql.Identifier(schema),sql.Identifier(APP)))
            conn.execute(sql.SQL('REVOKE INSERT,UPDATE,DELETE ON platform.schema_migrations FROM {}').format(sql.Identifier(APP)))
            conn.execute(sql.SQL('REVOKE CREATE ON SCHEMA public,platform,execution FROM {}').format(sql.Identifier(APP)))
        else:raise Refused('COMMAND_REFUSED')
    return {'schema':'maimi-r78-database-bootstrap/v1','status':'PASS','operation':command,
            'independent_test_database':True,'application_superuser':False,'credentials_returned':False}
def main():
    parser=argparse.ArgumentParser();g=parser.add_mutually_exclusive_group(required=True)
    g.add_argument('--create-roles',action='store_true');g.add_argument('--grant-application',action='store_true')
    args=parser.parse_args()
    try:
        report=run('create-roles' if args.create_roles else 'grant-application');code=0
    except Exception as error:
        report={'schema':'maimi-r78-database-bootstrap/v1','status':'REFUSED','error':str(error) if isinstance(error,Refused) else type(error).__name__,'credentials_returned':False};code=2
    print(json.dumps(report,sort_keys=True));return code
if __name__=='__main__':raise SystemExit(main())
