// Development-only loopback desktop adapter; contains no company identity or key.
import { spawn } from 'node:child_process';
import http from 'node:http';
import net from 'node:net';
import { mkdir, writeFile, readFile } from 'node:fs/promises';

const base = 'http://127.0.0.1:3085';
const authority = '127.0.0.1:28084';
const release = JSON.parse(await readFile('/opt/megmeet/release-manifest.json', 'utf8'));
let child, server, cookie = '', ready = false;
const delay = ms => new Promise(r => setTimeout(r, ms));
for (const d of ['/state/work', '/state/dsh', '/state/home', '/state/cache']) await mkdir(d, {recursive:true});
const community=JSON.parse(await readFile('/opt/megmeet/community.patch.json','utf8'));
for(const p of community[0].insert){if(!/^@dshp\/(?:file-change-viewer|web-style|widget-kit|token-meter)$/.test(p.name))throw Error('community plugin not reviewed');p.name='/opt/dsh/node_modules/'+p.name+'/megmeet-guarded-host.mjs';}
await writeFile('/state/community-profile.json',JSON.stringify(community),{mode:0o600});
const args = ['--profile','web','--patch','/state/community-profile.json','--patch','/opt/megmeet/profile.patch.yaml','--host','127.0.0.1','--port','3085','--no-open','--trusted-host',authority];
child = spawn(process.execPath, ['--expose-internals','/opt/dsh/node_modules/@deepseek-ai/dsh/lib/bin.js',...args], {stdio:['ignore','pipe','pipe'], env:{...process.env, DEEPSEEK_API_KEY:'synthetic-local-model-fixture-only', DEEPSEEK_BASE_URL:'http://mock-company:8081/v1'}});
let startup = '';
child.stdout.on('data', d => { startup = (startup + d).slice(-32768); });
child.stderr.on('data', d => { startup = (startup + d).slice(-32768); });
child.on('exit', code => {
  ready = false; server?.close();
  const safe = startup.replace(/token=[A-Za-z0-9_-]+/g, 'token=[redacted]');
  console.error('DSH_CHILD_EXIT:', code, safe.split('\n').slice(-12).join('\n'));
  process.exit(code || 1);
});
process.on('SIGTERM', () => child.kill('SIGTERM'));
process.on('SIGINT', () => child.kill('SIGTERM'));
try {
  for (let i=0; i<120; i++) {
    const token = startup.match(/http:\/\/127\.0\.0\.1:3085\/\?token=([A-Za-z0-9_-]+)/)?.[1];
    if (token) {
      const r = await fetch(base+'/?token='+token, {redirect:'manual'});
      cookie = r.headers.getSetCookie().map(s=>s.split(';')[0]).join('; ');
      if (cookie) break;
    }
    await delay(500);
  }
  if (!cookie) throw new Error('DSH local authentication bootstrap failed');
  const r = await fetch(base+'/api/workspace/create', {method:'POST',headers:{Cookie:cookie, Origin:base,'Content-Type':'application/json'},body:JSON.stringify({type:'client-request',rpcId:'local-default-workspace',method:'workspace/create',payload:{args:{request:{path:'/state/work'}}}})});
  const value = await r.json();
  if (!r.ok || value.result?.ok !== true || !value.result.value?.workspace?.workspaceId) throw new Error('DSH workspace contract failed: '+JSON.stringify(value));
  await writeFile('/state/local-workspace.json', JSON.stringify({workspaceId:value.result.value.workspace.workspaceId, version:release.dsh}), {mode:0o600});
  ready = true;
  const allowed = req => req.headers.host === authority && (!req.headers.origin || req.headers.origin === 'http://'+authority);
  const headers = req => ({...req.headers, host:'127.0.0.1:3085', origin:base, cookie, authorization:undefined});
  server = http.createServer((req,res) => {
    if (req.url === '/__megmeet/health' && req.method === 'GET') { res.setHeader('Content-Type','application/json'); res.end(JSON.stringify({status:ready?'ready':'starting',dsh:release.dsh,frontend:release.frontend,releaseId:release.releaseId,mode:'local-simulation'})); return; }
    if (!allowed(req)) { res.writeHead(403); res.end('local desktop origin required'); return; }
    if (req.url === '/__megmeet/release' && req.method === 'GET') { res.setHeader('Content-Type','application/json'); res.end(JSON.stringify(release)); return; }
    const h = headers(req); delete h.authorization;
    const up = http.request(base+req.url, {method:req.method, headers:h}, out => {
      const oh = {...out.headers}; delete oh['set-cookie'];
      res.writeHead(out.statusCode, oh); out.pipe(res);
    });
    up.on('error',()=>{if (!res.headersSent) res.writeHead(502); res.end('local DSH unavailable');});
    req.pipe(up);
  });
  server.on('upgrade',(req,socket,head)=>{
    if (!allowed(req) || req.url.split('?')[0] !== '/api/remote.mux') {socket.end('HTTP/1.1 403 Forbidden\r\n\r\n');return;}
    const upstream = net.connect(3085,'127.0.0.1',()=>{
      const h = headers(req); delete h.authorization;
      upstream.write(req.method+' '+req.url+' HTTP/1.1\r\n'+Object.entries(h).filter(([,v])=>v!==undefined).map(([k,v])=>k+': '+v).join('\r\n')+'\r\n\r\n');
      if (head.length) upstream.write(head);
      socket.pipe(upstream).pipe(socket);
    });
    upstream.on('error',()=>socket.destroy()); socket.on('error',()=>upstream.destroy());
    socket.on('close',()=>upstream.destroy()); upstream.on('close',()=>socket.destroy());
  });
  server.listen(3084,'0.0.0.0',()=>console.log('LOCAL_DSH_READY: 0.1.6-alpha.2; synthetic model; default workspace'));
} catch (error) {
  console.error(error.message);
  // Startup output may contain the ephemeral DSH token; never print it.
  child.kill('SIGTERM'); process.exitCode = 1;
}
