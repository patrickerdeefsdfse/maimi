"""Private card-v1-only helper; no native runtime, legacy purge or model calls."""
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import subprocess
import sys

TOKEN = os.getenv('CLOUD_ROUTE_TOKEN', '')
OWNER = os.getenv('CLOUD_OWNER_KEY', '')
MODE = os.getenv('CLOUD_MAINTENANCE', '')


def worker_identity(env):
    pod, generation = env.get('CLOUD_POD_UID', ''), env.get('CLOUD_WORKER_GENERATION', '')
    if not pod and not generation:
        return {}
    if (not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', pod)
        or not re.fullmatch(r'[1-9][0-9]{0,9}', generation) or int(generation) > 2147483647):
        raise ValueError('CARD_HELPER_IDENTITY_INVALID')
    return {'pod_uid': pod, 'generation': int(generation)}


IDENTITY = worker_identity(os.environ)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, value):
        raw = json.dumps(value, sort_keys=True, separators=(',',':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization','').encode(), ('Bearer '+TOKEN).encode())

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {'error':'UNAUTHORIZED'})
        if self.path == '/__cloud/ready':
            return self.reply(200, {'mode':MODE, **IDENTITY})
        return self.reply(404, {'error':'NOT_FOUND'})

    def do_POST(self):
        if not self.authorized():
            return self.reply(401, {'error':'UNAUTHORIZED'})
        if self.path != '/__cloud/projects/card-expire':
            return self.reply(404, {'error':'NOT_FOUND'})
        try:
            if self.headers.get('Transfer-Encoding') or self.headers.get('Origin') or self.headers.get('Sec-Fetch-Site'):
                raise ValueError()
            self.connection.settimeout(5)
            length = self.headers.get('Content-Length', '')
            if not re.fullmatch(r'[0-9]{1,7}', length) or not 1 <= int(length) <= 32768:
                raise ValueError()
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {'protocol','refs'} or value['protocol'] != 'card-retention-v1':
                raise ValueError()
            body = (json.dumps({'refs':value['refs']}, separators=(',',':'), allow_nan=False)+'\n').encode()
            process = subprocess.run([sys.executable,'-I','-B','/opt/personal-cloud/card_store.py',
                '--root','/state/dsh','--owner',OWNER,'--operation','expire'],
                input=body, capture_output=True, timeout=70,
                env={'PATH':'/usr/local/bin:/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'})
            if len(process.stdout) > 65536 or len(process.stderr) > 4096:
                raise ValueError()
            result = json.loads(process.stdout)
            if process.returncode == 2 and result == {'status':'ERROR','code':'BUSY'}:
                return self.reply(409, {'error':'BUSY'})
            if (not isinstance(result, dict)
                or (process.returncode,result.get('status')) not in ((0,'COMPLETE'),(2,'PARTIAL'))):
                raise ValueError()
            return self.reply(200, result)
        except (ValueError, TypeError, OSError, subprocess.TimeoutExpired):
            return self.reply(503, {'error':'CARD_CLEANUP_UNAVAILABLE'})


def main():
    if (not re.fullmatch(r'[A-Za-z0-9_-]{43}', TOKEN)
        or not re.fullmatch(r'[a-f0-9]{32}', OWNER) or MODE != 'card-retention-v1'):
        raise SystemExit('CARD_CLEANUP_CONFIGURATION')
    HTTPServer(('0.0.0.0',3084), Handler).serve_forever()


if __name__ == '__main__':
    main()
