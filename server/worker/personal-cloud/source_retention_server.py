"""Fixed offline files helper. The sole writable business mount is /workspace."""
import hmac
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
import re
import sys

# Isolated Python deliberately omits the script directory; this fixed image
# path supplies only the reviewed companion contract and store modules.
sys.path.insert(0,'/opt/personal-cloud')
from source_retention_contract import PROTOCOL,MAX_REQUEST_BYTES,validate_request
from source_retention_store import SourceStore
from attachment_cache_retention import CacheStore,request as cache_request


def handler(token,identity,store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,value):
            data=json.dumps(value,separators=(',',':'),sort_keys=True).encode()
            self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
        def authorized(self):return hmac.compare_digest(self.headers.get('Authorization','').encode(),('Bearer '+token).encode())
        def do_GET(self):
            if not self.authorized():return self.reply(401,{'error':'UNAUTHORIZED'})
            if self.path!='/__cloud/ready':return self.reply(404,{'error':'NOT_FOUND'})
            return self.reply(200,{'mode':PROTOCOL,**identity})
        def do_POST(self):
            if not self.authorized():return self.reply(401,{'error':'UNAUTHORIZED'})
            if self.path not in ('/__cloud/files/expire','/__cloud/files/cache-expire'):return self.reply(404,{'error':'NOT_FOUND'})
            try:
                if any(self.headers.get(k) for k in ('Origin','Sec-Fetch-Site','Transfer-Encoding')):raise ValueError()
                length=self.headers.get('Content-Length','')
                if not re.fullmatch(r'[1-9][0-9]{0,6}',length) or int(length)>MAX_REQUEST_BYTES:raise ValueError()
                self.connection.settimeout(5);raw=self.rfile.read(int(length))
                if len(raw)!=int(length):raise ValueError()
                if self.path.endswith('/cache-expire'):
                    body=cache_request(json.loads(raw))
                    if body['owner']!=os.getenv('CLOUD_OWNER_KEY'):raise ValueError()
                    return self.reply(200,CacheStore(str(store.root)).execute(body))
                body=validate_request(json.loads(raw))
                return self.reply(200,store.execute(body))
            except (ValueError,TypeError,OSError):return self.reply(503,{'error':'SOURCE_CLEANUP_UNCONFIRMED'})
    return Handler


def main():
    token=os.getenv('CLOUD_ROUTE_TOKEN','');owner=os.getenv('CLOUD_OWNER_KEY','')
    pod=os.getenv('CLOUD_POD_UID','');generation=os.getenv('CLOUD_WORKER_GENERATION','')
    if (os.getenv('CLOUD_MAINTENANCE')!=PROTOCOL or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token)
        or not re.fullmatch(r'[a-f0-9]{32}',owner) or not re.fullmatch(r'[1-9][0-9]{0,9}',generation)
        or int(generation)>2147483647 or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',pod)):
        raise SystemExit('SOURCE_HELPER_CONFIGURATION_INVALID')
    HTTPServer(('0.0.0.0',3084),handler(token,{'pod_uid':pod,'generation':int(generation)},SourceStore())).serve_forever()


if __name__=='__main__':main()
