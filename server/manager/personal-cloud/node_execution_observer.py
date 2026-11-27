"""Authenticated, read-only node observations used AFTER Kubernetes deletion.

API absence alone does not authorize a new writer. The selected node must report
two complete CRI/cgroup/Pod-directory observations. Missing node access, unknown
creation outcomes and incomplete reads retain the PostgreSQL reservation.
This is not remote fencing and cannot make a failed sandbox node highly available.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

SCHEMA = 'maimi-node-termination/v1'
MAX_BODY = 16384
MAX_AGE = 30
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')


class ObservationError(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise ObservationError(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def identity(handle):
    handle = asdict(handle) if is_dataclass(handle) else dict(handle)
    if handle.get('network_mode') is None:
        handle.pop('network_mode', None)
    result = {k: handle[k] for k in ('namespace','instance','owner','generation','name','pod_uid','node_name')}
    need(all(isinstance(result[k], str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}',result[k])
             for k in ('namespace','instance','name','node_name')), 'IDENTITY_NAME_INVALID')
    need(isinstance(result['owner'],str) and re.fullmatch('[a-f0-9]{32}',result['owner']), 'OWNER_INVALID')
    need(type(result['generation']) is int and 1 <= result['generation'] <= 2147483647, 'GENERATION_INVALID')
    need(isinstance(result['pod_uid'],str) and UUID.fullmatch(result['pod_uid']), 'POD_UID_INVALID')
    result['handle_sha256'] = hashlib.sha256(canonical(handle)).hexdigest()
    return result


def read_key(path):
    with open(path, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        need(stat.S_ISREG(meta.st_mode) and meta.st_size <= 256, 'KEY_FILE_INVALID')
        if os.name == 'posix':
            need(meta.st_uid in (0,os.geteuid()) and not meta.st_mode & 0o027, 'KEY_PERMISSIONS_INVALID')
        key = stream.read(257).strip()
    need(32 <= len(key) <= 128 and all(33 <= x <= 126 for x in key), 'KEY_FORMAT_INVALID')
    return key


def signed(body, key):
    return {'body': body, 'signature': hmac.new(key, canonical(body), hashlib.sha256).hexdigest()}


def authenticate(envelope, key):
    need(isinstance(envelope,dict) and set(envelope)=={'body','signature'}, 'ENVELOPE_INVALID')
    signature = envelope['signature']
    need(isinstance(signature,str) and re.fullmatch('[a-f0-9]{64}',signature), 'SIGNATURE_INVALID')
    need(hmac.compare_digest(signature,signed(envelope['body'],key)['signature']), 'SIGNATURE_INVALID')
    return envelope['body']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise ObservationError('OBSERVER_REDIRECT_REFUSED')


class ObserverClient:
    def __init__(self, url, key_loader, node_name, *, transport=None):
        parsed = urllib.parse.urlsplit(url)
        need(parsed.scheme in ('http','https') and parsed.hostname and parsed.path in ('','/')
             and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
             'OBSERVER_URL_INVALID')
        need(isinstance(node_name,str) and re.fullmatch('[a-z0-9][a-z0-9.-]{0,252}',node_name), 'OBSERVER_NODE_INVALID')
        self.url=url.rstrip('/')+'/v1/termination'
        self.key_loader=key_loader
        self.node_name=node_name
        self.pending={}
        self.transport=transport
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    @classmethod
    def from_env(cls):
        prefix='PERSONAL_CLOUD_K8S_OBSERVER_'
        path=os.environ[prefix+'KEY_FILE']
        return cls(os.environ[prefix+'URL'],lambda:read_key(path),
                   os.environ.get(prefix+'NODE_NAME',os.environ.get('PERSONAL_CLOUD_K8S_WORKER_NODE','')))

    async def __call__(self,handle):
        target=identity(handle)
        need(target['node_name']==self.node_name,'OBSERVER_NODE_MISMATCH')
        self.pending={n:v for n,v in self.pending.items() if time.monotonic()-v[1] <= MAX_AGE}
        need(len(self.pending)<4096,'OBSERVER_PENDING_LIMIT')
        nonce=uuid.uuid4().hex
        body={'schema':SCHEMA,'nonce':nonce,'requested_at':int(time.time()),'identity':target}
        envelope=signed(body,self.key_loader())
        self.pending[nonce]=(target,time.monotonic())
        try:
            if self.transport is not None:
                result=await self.transport(envelope)
            else:
                result=await asyncio.to_thread(self._request,envelope)
            self._verified(handle,result)
            return result
        except BaseException:
            self.pending.pop(nonce,None)
            raise

    def _request(self,envelope):
        request=urllib.request.Request(self.url,canonical(envelope),{'Content-Type':'application/json'},method='POST')
        try:
            with self.opener.open(request,timeout=15) as response:
                need(response.status==200,'OBSERVER_HTTP_FAILED')
                raw=response.read(MAX_BODY+1)
            need(len(raw)<=MAX_BODY,'OBSERVER_RESPONSE_LIMIT')
            return json.loads(raw)
        except ObservationError:
            raise
        except Exception:
            raise ObservationError('OBSERVER_UNAVAILABLE') from None

    def _verified(self,handle,proof):
        target=identity(handle)
        body=authenticate(proof,self.key_loader())
        need(isinstance(body,dict) and body.get('schema')==SCHEMA, 'PROOF_SCHEMA_INVALID')
        nonce=body.get('nonce')
        need(isinstance(nonce,str) and nonce in self.pending,'PROOF_NONCE_UNKNOWN')
        expected,started=self.pending[nonce]
        need(time.monotonic()-started <= MAX_AGE,'PROOF_EXPIRED')
        need(body.get('identity')==target==expected and target['node_name']==self.node_name,'PROOF_IDENTITY_MISMATCH')
        need(type(body.get('observed_at')) is int and abs(time.time()-body['observed_at']) <= MAX_AGE,'PROOF_TIME_INVALID')
        need(isinstance(body.get('boot_id'),str) and UUID.fullmatch(body['boot_id']),'PROOF_BOOT_INVALID')
        need(isinstance(body.get('observation_id'),str) and UUID.fullmatch(body['observation_id']),'PROOF_AUDIT_INVALID')
        need(body.get('execution_gone') is True and body.get('samples')==2 and body.get('same_boot') is True
             and body.get('cri_absent') is True and body.get('pod_directory_absent') is True
             and body.get('cgroup_unpopulated') is True, 'EXECUTION_NOT_CONFIRMED_GONE')
        return body

    async def verify(self,record,proof):
        from worker_reservations import VerifiedTermination
        handle=record['handle'] if isinstance(record,dict) and 'handle' in record else record
        body=self._verified(handle,proof)
        return VerifiedTermination(body['identity']['owner'],body['identity']['generation'],
            body['identity']['handle_sha256'],body['observation_id'],hashlib.sha256(canonical(proof)).hexdigest())


class LocalNodeReader:
    """Only fixed CRI list operations, filesystem metadata and cgroup counters."""
    def __init__(self, *, cri_binary, cri_socket, kubelet_root, cgroup_root='/sys/fs/cgroup',
                 boot_id_path='/proc/sys/kernel/random/boot_id'):
        need(Path(cri_binary).is_absolute() and Path(kubelet_root).is_absolute(),'NODE_PATH_INVALID')
        need(re.fullmatch(r'unix:///[a-zA-Z0-9/_.-]+',cri_socket),'CRI_SOCKET_INVALID')
        self.command=[cri_binary,'crictl','--runtime-endpoint',cri_socket,'--timeout','5s']
        self.kubelet_root=Path(kubelet_root)
        self.cgroup_root=Path(cgroup_root)
        self.boot_id_path=Path(boot_id_path)

    def _cri(self,kind):
        args=['pods','--output','json'] if kind=='items' else ['ps','--all','--output','json']
        try:
            p=subprocess.run(self.command+args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,timeout=7,env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LANG':'C.UTF-8'},close_fds=True)
            need(p.returncode==0 and len(p.stdout)<=4*1024*1024,'CRI_QUERY_FAILED')
            value=json.loads(p.stdout)
            need(isinstance(value,dict) and kind in value and isinstance(value[kind],list),'CRI_SCHEMA_INVALID')
            need(len(value[kind])<=10000,'CRI_LIST_LIMIT')
            return value[kind]
        except ObservationError:
            raise
        except Exception:
            raise ObservationError('CRI_QUERY_FAILED') from None

    def _cgroups_clear(self,pod_uid):
        # Unified hierarchy required. Inspect every matching Pod cgroup rather
        # than guessing one QoS path; an unreadable traversal fails closed.
        need((self.cgroup_root/'cgroup.controllers').is_file(),'CGROUP_V2_REQUIRED')
        needles=('pod'+pod_uid,'pod'+pod_uid.replace('-','_'))
        count=0
        def failed(error):
            raise ObservationError('CGROUP_WALK_FAILED')
        for root,dirs,files in os.walk(self.cgroup_root,followlinks=False,onerror=failed):
            count+=1
            need(count<=10000,'CGROUP_WALK_LIMIT')
            for name in dirs:
                need(not (Path(root)/name).is_symlink(),'CGROUP_SYMLINK_REFUSED')
            parts=Path(root).relative_to(self.cgroup_root).parts
            if not any(any(n==p or p.endswith('-'+n+'.slice') or p==n+'.slice' for n in needles) for p in parts):
                continue
            try:
                events=dict(line.split() for line in (Path(root)/'cgroup.events').read_text().splitlines())
            except FileNotFoundError:
                # Removal during traversal is ambiguous; a later observation
                # can establish absence without interpreting this race.
                raise ObservationError('CGROUP_CHANGED_DURING_READ') from None
            need(events.get('populated')=='0','POD_CGROUP_POPULATED')
        return True

    def sample(self,target):
        boot=self.boot_id_path.read_text().strip()
        need(UUID.fullmatch(boot),'NODE_BOOT_ID_INVALID')
        for kind in ('items','containers'):
            for row in self._cri(kind):
                need(isinstance(row,dict) and isinstance(row.get('labels'),dict),'CRI_ITEM_INVALID')
                labels=row['labels']
                if labels.get('io.kubernetes.pod.uid')==target['pod_uid']:
                    raise ObservationError('POD_RUNTIME_REMAINS')
        pods=self.kubelet_root/'pods'
        need(pods.is_dir() and not pods.is_symlink(),'KUBELET_PODS_UNAVAILABLE')
        try:
            (pods/target['pod_uid']).lstat()
        except FileNotFoundError:
            pass
        else:
            raise ObservationError('POD_DIRECTORY_REMAINS')
        self._cgroups_clear(target['pod_uid'])
        return boot


class NodeObserver:
    def __init__(self,key_loader,node_name,instance,reader,*,interval=1.0):
        need(interval>=1,'OBSERVATION_INTERVAL_TOO_SHORT')
        self.key_loader=key_loader;self.node_name=node_name;self.instance=instance
        self.reader=reader;self.interval=interval

    def observe(self,envelope):
        request=authenticate(envelope,self.key_loader())
        need(isinstance(request,dict) and request.get('schema')==SCHEMA,'REQUEST_SCHEMA_INVALID')
        need(isinstance(request.get('nonce'),str) and re.fullmatch('[a-f0-9]{32}',request['nonce']),'REQUEST_NONCE_INVALID')
        need(type(request.get('requested_at')) is int and abs(time.time()-request['requested_at'])<=MAX_AGE,'REQUEST_EXPIRED')
        target=request.get('identity')
        need(isinstance(target,dict) and target.get('node_name')==self.node_name and target.get('instance')==self.instance,'REQUEST_NODE_MISMATCH')
        need(isinstance(target.get('pod_uid'),str) and UUID.fullmatch(target['pod_uid']),'POD_UID_INVALID')
        first=self.reader.sample(target)
        time.sleep(self.interval)
        second=self.reader.sample(target)
        need(first==second,'NODE_REBOOTED_DURING_OBSERVATION')
        return signed({'schema':SCHEMA,'nonce':request['nonce'],'identity':target,
            'observed_at':int(time.time()),'boot_id':second,'observation_id':str(uuid.uuid4()),
            'execution_gone':True,'samples':2,'same_boot':True,'cri_absent':True,
            'pod_directory_absent':True,'cgroup_unpopulated':True},self.key_loader())


def serve_from_env():
    """Explicit node service entry point; never invoked by importing a manager."""
    key_path=os.environ['MAIMI_OBSERVER_KEY_FILE']
    observer=NodeObserver(lambda:read_key(key_path),os.environ['MAIMI_OBSERVER_NODE_NAME'],
        os.environ['MAIMI_OBSERVER_INSTANCE'],LocalNodeReader(cri_binary=os.environ['MAIMI_OBSERVER_K3S_BINARY'],
        cri_socket=os.environ['MAIMI_OBSERVER_CRI_SOCKET'],kubelet_root=os.environ['MAIMI_OBSERVER_KUBELET_ROOT']))
    import threading
    semaphore=threading.BoundedSemaphore(4)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            self.connection.settimeout(5)
            acquired=False
            try:
                need(self.path=='/v1/termination' and not self.headers.get('Transfer-Encoding'),'REQUEST_REFUSED')
                length=int(self.headers.get('Content-Length','0'))
                need(0<length<=MAX_BODY,'REQUEST_SIZE_REFUSED')
                acquired=semaphore.acquire(blocking=False)
                need(acquired,'OBSERVER_BUSY')
                response=observer.observe(json.loads(self.rfile.read(length)))
                payload=canonical(response);code=200
            except Exception:
                payload=canonical({'status':'OBSERVATION_UNAVAILABLE'});code=503
            finally:
                if acquired:semaphore.release()
            self.send_response(code);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    bind=os.environ.get('MAIMI_OBSERVER_BIND','127.0.0.1')
    port=int(os.environ.get('MAIMI_OBSERVER_PORT','8096'))
    ThreadingHTTPServer((bind,port),Handler).serve_forever()


if __name__=='__main__':
    serve_from_env()
