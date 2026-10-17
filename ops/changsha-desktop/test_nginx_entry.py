"""Public routing contracts and actual Nginx parser; no company traffic."""
import importlib.util
import http.client
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
from uuid import uuid4

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('entry_renderer',HERE/'render-nginx-entry.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
DIST=HERE.parent/'desktop-distribution'

class EntryTests(unittest.TestCase):
    def test_complete_front_door_uses_two_distinct_upstreams_and_client_authority(self):
        output=module.render(HERE,DIST,True)
        self.assertIn('http://megmeet-desktop-20260918-gateway:8080;',output)
        self.assertIn('http://megmeet-maimi-downloads:8080;',output)
        self.assertNotIn('proxy_pass http://127.0.0.1:',output)
        self.assertIn('location ^~ /v1/connectors/',output)
        self.assertIn('location ^~ /maimi/web/',output)
        self.assertIn('location ^~ /maimi/',output)
        for endpoint in ('/api/remote.mux','/v1/personal-cloud/native/api/remote.mux'):
            block=output.split('location = '+endpoint+' {',1)[1].split('}',1)[0]
            self.assertIn('proxy_set_header Upgrade $http_upgrade;',block)
            self.assertIn('proxy_set_header Origin $http_origin;',block)
        connectors=output.split('location ^~ /v1/connectors/ {',1)[1].split('}',1)[0]
        self.assertIn('proxy_set_header Authorization $http_authorization;',connectors)
        self.assertIn('proxy_set_header Cookie $http_cookie;',connectors)
        downloads=output.split('location ^~ /maimi/ {',1)[1].split('}',1)[0]
        self.assertIn('proxy_set_header Authorization "";',downloads)
        self.assertIn('proxy_set_header Cookie "";',downloads)
        self.assertEqual(output.count('auth_request off;'),len(re.findall(r'(?m)^[ \t]*location ',output)))

    @unittest.skipUnless(shutil.which('docker') and Path('/var/run/docker.sock').exists(),'local Linux Docker parser test')
    def test_real_nginx_accepts_combined_fragment_with_inherited_legacy_auth(self):
        fragment=module.render(HERE,DIST).replace('127.0.0.1:18091','127.0.0.1:18093').replace('127.0.0.1:18092','127.0.0.1:18094')
        content='events {}\nhttp { server { listen 8080; auth_request /legacy-authorization;\n'+fragment+'\n} }\n'
        with tempfile.TemporaryDirectory(prefix='megmeet-entry-parser-') as directory:
            path=Path(directory)/'nginx.conf';path.write_text(content)
            result=subprocess.run(['docker','--host','unix:///var/run/docker.sock','run','--rm','--network','none',
              '--volume',str(path)+':/etc/nginx/nginx.conf:ro','nginx:alpine','nginx','-t'],capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('docker') and Path('/var/run/docker.sock').exists(),'local Linux Docker transport test')
    def test_actual_public_routes_preserve_gateway_authority_and_strip_download_authority(self):
        docker=['docker','--host','unix:///var/run/docker.sock']
        image='megmeet-ai-gateway:alpha2-candidate-20260920'
        if subprocess.run(docker+['image','inspect',image],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            self.skipTest('verified local candidate fixture image unavailable')
        name='megmeet-nginx-route-'+uuid4().hex[:12]
        backend=name+'-backend';edge=name+'-edge'
        def run(*args):return subprocess.check_output(docker+list(args),text=True,stderr=subprocess.STDOUT).strip()
        # The backend reports only equality booleans, never credential values.
        script='''from http.server import BaseHTTPRequestHandler,HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_GET(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')))
  data=json.dumps({'path':self.path,'auth':self.headers.get('Authorization')=='Bearer synthetic-only','cookie':self.headers.get('Cookie')=='synthetic-cookie=fixture','origin':self.headers.get('Origin')=='https://platform-ai.megmeet.com'}).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 do_POST=do_GET
 do_PUT=do_GET
HTTPServer(('0.0.0.0',8080),Handler).serve_forever()
'''
        try:
            run('network','create',name)
            run('run','--detach','--name',backend,'--network',name,'--network-alias',module.GATEWAY_ALIAS,
                '--network-alias',module.DOWNLOAD_ALIAS,image,'python','-c',script)
            with tempfile.TemporaryDirectory(prefix='megmeet-entry-transport-') as directory:
                path=Path(directory)/'nginx.conf'
                path.write_text('events {}\nhttp { server { listen 8080; auth_request /old-auth;\n'+module.render(HERE,DIST,True)+'\n} }')
                run('run','--detach','--name',edge,'--network',name,'--publish','127.0.0.1::8080',
                    '--volume',str(path)+':/etc/nginx/nginx.conf:ro','nginx:alpine')
                address=run('port',edge,'8080/tcp');self.assertTrue(address.startswith('127.0.0.1:'))
                client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def request(endpoint,method='GET'):
                    value=urllib.request.Request('http://'+address+endpoint,method=method,
                        headers={'Authorization':'Bearer synthetic-only','Cookie':'synthetic-cookie=fixture','Origin':'https://platform-ai.megmeet.com'})
                    with client.open(value,timeout=4) as response:return json.load(response)
                for attempt in range(30):
                    try:request('/health');break
                    except OSError:
                        if attempt==29:raise
                        time.sleep(.1)
                for endpoint,method in [('/v1/connectors/catalog','GET'),('/v1/connectors/invoke','POST'),
                    ('/v1/connectors/confirm','POST'),('/v1/connectors/dingtalk/callback?code=synthetic&state=fixture','GET'),
                    ('/maimi/web/','GET'),('/api/session/prompt','POST'),('/__megmeet/tools/confirm','POST'),
                    ('/v1/office/capabilities','GET'),('/v1/office/jobs','POST'),
                    ('/v1/office/jobs/'+'a'*32+'/inputs/0','PUT')]:
                    result=request(endpoint,method)
                    self.assertEqual(result,{'path':endpoint,'auth':True,'cookie':True,'origin':True},endpoint)
                result=request('/maimi/')
                self.assertEqual(result,{'path':'/maimi/','auth':False,'cookie':False,'origin':True})
                for endpoint in ('/v1/office','/internal/personal-cloud/office','/internal/personal-cloud/office/jobs'):
                    with self.assertRaises(urllib.error.HTTPError) as caught:request(endpoint)
                    self.assertEqual(caught.exception.code,404)
                def declared_size(endpoint,size):
                    connection=http.client.HTTPConnection(address,timeout=5)
                    try:
                        connection.putrequest('PUT' if '/inputs/' in endpoint else 'POST',endpoint)
                        connection.putheader('Content-Length',str(size));connection.endheaders()
                        response=connection.getresponse();status=response.status;response.read();return status
                    finally:connection.close()
                upload='/v1/office/jobs/'+'b'*32+'/inputs/0'
                self.assertEqual(declared_size('/v1/office/jobs',2*1024**2+1),413)
                self.assertEqual(declared_size(upload,64*1024**2+1),413)
                value=urllib.request.Request('http://'+address+upload,data=b'x'*(3*1024**2),method='PUT',
                    headers={'Authorization':'Bearer synthetic-only','Cookie':'synthetic-cookie=fixture','Origin':'https://platform-ai.megmeet.com'})
                with client.open(value,timeout=6) as response:
                    self.assertEqual(json.load(response),{'path':upload,'auth':True,'cookie':True,'origin':True})
        finally:
            for resource in (edge,backend):
                subprocess.run(docker+['rm','--force',resource],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            subprocess.run(docker+['network','rm',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

    @unittest.skipUnless(os.name=='posix' and shutil.which('bash'),'local shell argument test')
    def test_combined_start_validates_first_and_handles_download_directory_spaces(self):
        with tempfile.TemporaryDirectory(prefix='megmeet-start-entry-') as temporary:
            root=Path(temporary);source=root/'server';source.mkdir()
            downloads=root/'download fixture';downloads.mkdir();binaries=root/'bin';binaries.mkdir()
            shutil.copy2(HERE/'start-server.sh',source/'start-server.sh')
            (downloads/'nginx-maimi.locations.conf').write_text('# synthetic')
            (downloads/'start-distribution.sh').write_text('#!/bin/sh\nprintf "DOWNLOAD:%s\\n" "$*" >> "$ENTRY_TEST_LOG"\n')
            for name in ('docker','python3'):
                path=binaries/name
                path.write_text('#!/bin/sh\nprintf "'+name+':%s\\n" "$*" >> "$ENTRY_TEST_LOG"\n')
                path.chmod(0o755)
            log=root/'commands.log';env={**os.environ,'PATH':str(binaries)+':'+os.environ['PATH'],'ENTRY_TEST_LOG':str(log)}
            result=subprocess.run(['bash',str(source/'start-server.sh'),'--nginx-network','--distribution-dir',str(downloads)],env=env,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            lines=log.read_text().splitlines()
            distribution_check=next(i for i,line in enumerate(lines) if line=='python3:verify-distribution.py')
            image_check=next(i for i,line in enumerate(lines) if line.startswith('python3:verify-images.py'))
            startup=next(i for i,line in enumerate(lines) if line.startswith('docker:compose ') and ' up -d ' in line)
            self.assertLess(distribution_check,startup);self.assertLess(image_check,startup)
            self.assertEqual(lines[-1],'DOWNLOAD:--nginx-network')
            self.assertTrue(any(line.startswith('python3:render-nginx-entry.py ') and '--container-nginx' in line for line in lines))
            self.assertIn('NGINX_CONFIG_CHANGED: NO',result.stdout)
            log.write_text('')
            bad=subprocess.run(['bash',str(source/'start-server.sh'),'--distribution-dir',str(root/'missing')],env=env,capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertNotIn(' up -d ',log.read_text())

if __name__=='__main__':unittest.main()
