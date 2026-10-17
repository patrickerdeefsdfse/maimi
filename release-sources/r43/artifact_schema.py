"""Read-only artifact-table catalog checks, including the exact byte guard.

The release controller additionally verifies the 016 migration ledger and all
other source/schema pins. This helper never reads an artifact business row.
"""
import hashlib
import json
from pathlib import Path

HERE=Path(__file__).resolve().parent

def read_artifact_schema(cur):
    cur.execute("""SELECT c.relkind,c.relpersistence,c.relrowsecurity,c.relforcerowsecurity
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='platform' AND c.relname='project_artifacts'""")
    table=cur.fetchone()
    if not table:raise RuntimeError('ARTIFACT_TABLE_ABSENT')
    cur.execute("""SELECT a.attname AS name,pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
      a.attnotnull AS not_null,pg_catalog.pg_get_expr(d.adbin,d.adrelid) AS default
      FROM pg_catalog.pg_attribute a
      JOIN pg_catalog.pg_class t ON t.oid=a.attrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
      LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
      WHERE n.nspname='platform' AND t.relname='project_artifacts'
        AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum""")
    columns=cur.fetchall()
    cur.execute("""SELECT c.conname AS name,c.contype AS type,
      pg_catalog.pg_get_constraintdef(c.oid,false) AS definition,
      c.convalidated AS validated,c.condeferrable AS deferrable,c.condeferred AS initially_deferred
      FROM pg_catalog.pg_constraint c
      JOIN pg_catalog.pg_class t ON t.oid=c.conrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
      WHERE n.nspname='platform' AND t.relname='project_artifacts' ORDER BY c.conname""")
    constraints=cur.fetchall()
    cur.execute("""SELECT g.tgname AS name,pg_catalog.pg_get_triggerdef(g.oid,false) AS definition
      FROM pg_catalog.pg_trigger g
      JOIN pg_catalog.pg_class t ON t.oid=g.tgrelid
      JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace
      WHERE n.nspname='platform' AND t.relname='project_artifacts' AND NOT g.tgisinternal ORDER BY g.tgname""")
    triggers=cur.fetchall()
    return {'table':dict(table),'columns':[dict(x) for x in columns],
            'constraints':[dict(x) for x in constraints],'user_triggers':[dict(x) for x in triggers]}

def schema_digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def classify_artifact_schema(cur,expected=None):
    if expected is None:expected=json.loads((HERE/'artifact-schema-expected.json').read_text(encoding='utf-8'))
    actual=read_artifact_schema(cur)
    for mode in ('ORIGINAL','MIGRATED'):
        if actual==expected[mode]:return {'mode':mode,'schema_sha256':schema_digest(actual)}
    raise RuntimeError('ARTIFACT_SCHEMA_MISMATCH')
