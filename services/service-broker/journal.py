"""Private durable idempotency journal; never retries an SSH side effect."""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from uuid import uuid4
from protocol import PROTOCOL,TERMINAL,Rejected,dumps,require

class Journal:
    def __init__(self,filename,*,clock):
        self.filename=Path(filename);self.clock=clock
        require(self.filename.parent.is_dir() and not any(p.is_symlink() for p in (self.filename,*self.filename.parents)),'NOT_CONFIGURED',503)
        parent=self.filename.parent.stat();uid=getattr(os,'geteuid',lambda:0)()
        require(os.name=='nt' or parent.st_uid in {0,uid} and stat.S_IMODE(parent.st_mode)==0o700,'NOT_CONFIGURED',503)
        if not self.filename.exists():
            fd=os.open(self.filename,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        info=self.filename.stat();require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and (os.name=='nt' or stat.S_IMODE(info.st_mode)==0o600 and info.st_uid in {0,uid}),'NOT_CONFIGURED',503)
        self.db=sqlite3.connect(str(self.filename),isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA max_page_count=16384');self.db.execute('PRAGMA secure_delete=ON')
        self.db.execute('CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,fingerprint TEXT NOT NULL,employee TEXT NOT NULL,scope TEXT NOT NULL,action TEXT NOT NULL,status TEXT NOT NULL,result TEXT,error_code TEXT,created INTEGER NOT NULL)')
        self.db.execute("UPDATE operations SET status='unknown',result=NULL,error_code='RESTART_UNKNOWN' WHERE status IN ('queued','running','cancelling')")
        self.db.execute('CREATE TABLE IF NOT EXISTS public_host_keys(employee TEXT NOT NULL,address TEXT NOT NULL,port INTEGER NOT NULL,public_key TEXT NOT NULL,PRIMARY KEY(employee,address,port))')
        self.db.execute('CREATE TABLE IF NOT EXISTS retired_connections(scope_hash TEXT PRIMARY KEY,expires INTEGER NOT NULL)')
    def _connection_key(self,employee,session,generation):
        return hashlib.sha256(dumps([employee,session,generation]).encode()).hexdigest()
    def connection_retired(self,employee,session,generation):
        row=self.db.execute('SELECT expires FROM retired_connections WHERE scope_hash=?',(self._connection_key(employee,session,generation),)).fetchone()
        return row is not None and row['expires']>self.clock()
    def retire_connection(self,employee,session,generation):
        # Durable before acknowledging revocation. A wire scope is valid for at
        # most 665 seconds; retain the tombstone for 900 so every already admitted
        # request has expired before removal. Restart never reopens that window.
        key=self._connection_key(employee,session,generation)
        self.db.execute('BEGIN IMMEDIATE')
        try:
            self.db.execute('DELETE FROM retired_connections WHERE expires<=?',(int(self.clock()),))
            count=self.db.execute('SELECT count(*) FROM retired_connections').fetchone()[0]
            require(count<10000 or self.connection_retired(employee,session,generation),'RATE_LIMIT',429)
            self.db.execute('INSERT INTO retired_connections VALUES(?,?) ON CONFLICT(scope_hash) DO UPDATE SET expires=MAX(expires,excluded.expires)',(key,int(self.clock())+900))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK');raise
    def check_host_key(self,employee,address,port,public_key):
        # Public SSH identity only. Never persists passwords or connection requests.
        row=self.db.execute('SELECT public_key FROM public_host_keys WHERE employee=? AND address=? AND port=?',(employee,address,port)).fetchone()
        if row:return row['public_key']==public_key
        require(self.db.execute('SELECT COUNT(*) FROM public_host_keys').fetchone()[0]<1000,'RATE_LIMIT',429)
        self.db.execute('INSERT INTO public_host_keys VALUES(?,?,?,?)',(employee,address,port,public_key))
        return True
    def close(self):self.db.close()
    def by_request(self,request_id):return self.db.execute('SELECT * FROM operations WHERE request_id=?',(request_id,)).fetchone()
    def insert(self,request,fingerprint):
        previous=self.by_request(request['request_id'])
        if previous:
            require(previous['fingerprint']==fingerprint,'IDEMPOTENCY_CONFLICT',409)
            return dict(previous),False
        count=self.db.execute('SELECT count(*) FROM operations').fetchone()[0]
        if count>=1000:
            # Only stale terminal rows expire; active/uncertain receipts are retained.
            self.db.execute("DELETE FROM operations WHERE created<? AND status IN ('completed','failed','cancelled')",(int(self.clock())-86400,))
            require(self.db.execute('SELECT count(*) FROM operations').fetchone()[0]<1000,'RATE_LIMIT',429)
        operation=str(uuid4())
        self.db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,NULL,NULL,?)',(operation,request['request_id'],fingerprint,request['employee_no'],dumps(request['scope']),request['action'],'queued',int(self.clock())))
        return dict(self.db.execute('SELECT * FROM operations WHERE operation_id=?',(operation,)).fetchone()),True
    def get(self,operation,employee,job_id):
        row=self.db.execute('SELECT * FROM operations WHERE operation_id=?',(operation,)).fetchone()
        require(row is not None,'SCOPE_MISMATCH',404)
        require(row['employee']==employee and row['request_id']==job_id,'SCOPE_MISMATCH',403)
        return dict(row)
    def state(self,operation,status,result=None,error_code=None):
        require(status in TERMINAL|{'queued','running','cancelling'})
        row=self.db.execute('SELECT status FROM operations WHERE operation_id=?',(operation,)).fetchone()
        require(row is not None,'SCOPE_MISMATCH',404)
        if row['status'] in TERMINAL:return
        self.db.execute('UPDATE operations SET status=?,result=?,error_code=? WHERE operation_id=?',(status,None if result is None else dumps(result),error_code,operation))
    @staticmethod
    def view(row,*,initial=False):
        scope=json.loads(row['scope'])
        value={'protocol':PROTOCOL,'operation_id':row['operation_id'],'status':row['status'],'scope':scope,'expires_at':scope['expires_at']}
        if not initial:value.update({'result':None if row['result'] is None else json.loads(row['result']),'error_code':row['error_code']})
        return value
