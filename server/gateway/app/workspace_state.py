"""Candidate PG workspace-domain authority; no HTTP or production activation.

Identity must come from the existing authenticated worker capability, never a
browser body. A reservation SHARE lock covers every read/write transaction and
therefore serializes its commit with the existing trusted release operation.
This does not release a generation or prove that an old NAS writer has stopped.
"""
from __future__ import annotations
import hashlib,json,re
from uuid import UUID
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

class WorkspaceConflict(Exception): pass
def need(ok,code):
    if not ok:raise WorkspaceConflict(code)
def identity(owner,generation):
    need(isinstance(owner,str) and re.fullmatch('[a-f0-9]{32}',owner),'INVALID_OWNER')
    need(type(generation) is int and generation>0,'INVALID_GENERATION')
def encode(value):
    need(type(value) is dict,'INVALID_WORKSPACE_DOCUMENT')
    try:raw=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False,ensure_ascii=False).encode()
    except (ValueError,TypeError,UnicodeError,RecursionError):raise WorkspaceConflict('INVALID_WORKSPACE_DOCUMENT') from None
    need(len(raw)<=4*1024**2,'WORKSPACE_DOCUMENT_TOO_LARGE')
    # Canonical round trip rejects Python-only types and non-string map keys.
    need(json.loads(raw)==value,'INVALID_WORKSPACE_DOCUMENT')
    return raw

class WorkspaceStateStore:
    def __init__(self,connect):self.connect=connect
    def _fence(self,cur,owner,generation):
        cur.execute("SELECT generation FROM platform.worker_runtime_reservations WHERE owner_key=%s AND generation=%s AND state IN ('BOUND','READY') AND purpose='worker' AND handle IS NOT NULL FOR SHARE",(owner,generation))
        need(cur.fetchone() is not None,'WORKSPACE_GENERATION_NOT_ADMITTED')
    def load(self,owner,generation):
        identity(owner,generation)
        with self.connect() as conn,conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout='10s'")
            self._fence(cur,owner,generation)
            cur.execute('SELECT revision,value,unit_name,format_version FROM platform.native_workspace_state WHERE owner_key=%s',(owner,));row=cur.fetchone()
            need(row is None or (row['unit_name']=='workspace' and row['format_version']==2),'WORKSPACE_FORMAT_MISMATCH')
            return {'revision':str(row['revision']),'value':row['value']} if row else {'revision':'0','value':{}}
    def compare_and_swap(self,owner,generation,expected_revision,write_id,value):
        identity(owner,generation)
        need(type(expected_revision) is int and 0<=expected_revision<2**63-1,'INVALID_REVISION')
        try:write_id=str(UUID(write_id))
        except (ValueError,TypeError,AttributeError):raise WorkspaceConflict('INVALID_WRITE_ID') from None
        raw=encode(value);digest=hashlib.sha256(raw).hexdigest()
        with self.connect() as conn,conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET LOCAL statement_timeout='10s'")
            self._fence(cur,owner,generation)
            cur.execute('INSERT INTO platform.native_workspace_state(owner_key) VALUES(%s) ON CONFLICT DO NOTHING',(owner,))
            cur.execute('SELECT * FROM platform.native_workspace_state WHERE owner_key=%s FOR UPDATE',(owner,));row=cur.fetchone()
            need(row['unit_name']=='workspace' and row['format_version']==2,'WORKSPACE_FORMAT_MISMATCH')
            if row['last_write_id'] is not None and str(row['last_write_id'])==write_id:
                need(row['writer_generation']==generation and row['revision']==expected_revision+1 and row['content_sha256']==digest and row['value']==value,'WRITE_ID_CONFLICT')
                return {'revision':str(row['revision']),'acknowledged':True,'idempotent':True}
            need(row['revision']==expected_revision,'WORKSPACE_REVISION_CONFLICT')
            cur.execute('UPDATE platform.native_workspace_state SET revision=revision+1,value=%s,writer_generation=%s,last_write_id=%s,content_sha256=%s,updated_at=clock_timestamp() WHERE owner_key=%s RETURNING revision',(Jsonb(value),generation,write_id,digest,owner))
            revision=cur.fetchone()['revision']
            return {'revision':str(revision),'acknowledged':True,'idempotent':False}
