const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto'), cp = require('node:child_process');
const root = path.resolve(__dirname, '../..'), desktop = path.join(root, 'desktop-local'), runtime = path.join(desktop, 'runtime');
function replace(file, a, b) { const f=path.join(root,file),s=fs.readFileSync(f,'utf8'); if(!s.includes(a)&&!s.includes(b))throw Error(file);fs.writeFileSync(f,s.replaceAll(a,b)); }
for(const f of ['desktop-local/package.json','desktop-local/package-lock.json']) replace(f,'"version": "0.4.1"','"version": "0.4.2"');
replace('desktop-local/scripts/audit-packaged-candidate.cjs',"version, '0.4.1'", "version, '0.4.2'");
replace('dev-local/dsh/brand-overlay-r2.mjs',"enterpriseAdapter: { version: '3.0.0'", "enterpriseAdapter: { version: '3.1.0'");
replace('packages/dsh-adapter/index.cjs',"set.extension.version === '3.0.0'", "set.extension.version === '3.1.0'");
for (const name of ['brand-overlay-r2.mjs','enterprise-native.mjs','workspace-pinned-overlay.mjs','prepare-runtime.mjs']) fs.copyFileSync(path.join(root,'dev-local/dsh',name),path.join(runtime,name));
const modules=path.join(runtime,'node_modules/@deepseek-ai');
for(const name of ['dsh-client-ui-brand-official','dsh-client-ui-conversation','dsh-client-ui-settings-models','dsh-client-ui-workspace']) fs.copyFileSync(path.join(runtime,'pristine',name+'.js'),path.join(modules,name,'lib/client.js'));
for(const [name,args] of [['brand-overlay-r2.mjs',[modules,path.join(runtime,'brand-adaptation.json')]],['prepare-runtime.mjs',[modules,runtime]]]) cp.execFileSync(process.execPath,[path.join(runtime,name),...args],{stdio:'inherit'});
const f=path.join(runtime,'runtime-manifest.json'),m=JSON.parse(fs.readFileSync(f));
for(const [name,key] of [['release-manifest.json','releaseSha256'],['brand-adaptation.json','adaptationSha256']]) m[key]=crypto.createHash('sha256').update(fs.readFileSync(path.join(runtime,name))).digest('hex');
fs.writeFileSync(f,JSON.stringify(m,null,2)+'\n');
cp.execFileSync(process.execPath,[path.join(desktop,'scripts/verify-dsh-candidate.cjs'),'--seal-local'],{stdio:'inherit'});
