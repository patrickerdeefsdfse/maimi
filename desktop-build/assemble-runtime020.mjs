// A fresh, exact npm 0.2.0-rc.2 tree; frozen prior releases remain read-only.
import fs from 'node:fs';import path from 'node:path';import {createHash} from 'node:crypto';import {createRequire} from 'node:module';import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),repo=path.resolve(root,'../../..');
const runtime='C:/Users/LI YUHAO/.codex/tmp/dsh020-r41-desktop/runtime-win32-x64';
const prior=path.join(repo,'output/remembered-login-r40-20260930/desktop/package-build-final/desktop-local/dist/0.12.0-company/MEGMEET AI-win32-x64/resources/runtime');
const app=path.join(root,'app'),version='0.2.0-rc.2',sha=b=>createHash('sha256').update(b).digest('hex'),json=f=>JSON.parse(fs.readFileSync(f)),require=createRequire(import.meta.url);
const check=(v,e)=>{if(!v)throw Error(e);};
const write=(rel,b)=>{const p=path.join(runtime,rel);fs.mkdirSync(path.dirname(p),{recursive:true});fs.writeFileSync(p,b);};
const writeJson=(rel,x)=>write(rel,JSON.stringify(x,null,2)+'\n');
const candidate=json(path.join(root,'candidate-ui.json')),oldRelease=json(path.join(prior,'release-manifest.json')),oldBrand=json(path.join(prior,'brand-adaptation.json'));
require(path.join(repo,'output/remembered-login-r40-20260930/desktop/app/dsh-adapter.cjs')).validateRuntime(prior,{full:true});
const packages={},scoped=path.join(runtime,'node_modules/@deepseek-ai');
for(const name of fs.readdirSync(scoped).filter(n=>n==='dsh'||n.startsWith('dsh-')).sort()){const pkg=json(path.join(scoped,name,'package.json'));check(pkg.name==='@deepseek-ai/'+name&&pkg.version===version,'MIXED_DSH:'+name);packages[pkg.name]=pkg.version;}
const lock=json(path.join(runtime,'package-lock.json'));check(lock.packages[''].dependencies['@deepseek-ai/dsh']===version,'ROOT_DSH_LOCK');
const lockedDsh=Object.entries(lock.packages).filter(([key])=>/(?:^|\/)node_modules\/@deepseek-ai\/dsh(?:-[^/]+)?$/.test(key));
for(const [key,item] of lockedDsh)check(item.version===version,'MIXED_NESTED_DSH_LOCK:'+key);
const lockDistribution=Object.fromEntries([...new Set(lockedDsh.map(([,item])=>item.version))].map(v=>[v,lockedDsh.filter(([,item])=>item.version===v).length]));
const sourcePins=json(path.join(root,'evidence/upstream-ui-acquisition.json'));
for(const row of candidate.adaptations){const rel='node_modules/'+row.package+'/lib/client.js',upstream=sourcePins.find(p=>p.name===row.package);check(upstream?.files['lib/client.js']===row.originalSha256,'UPSTREAM_METADATA');const before=sha(fs.readFileSync(path.join(runtime,rel)));check(before===row.originalSha256||before===row.adaptedSha256,'UI_PIN:'+row.package);const b=fs.readFileSync(path.join(root,'overlays',row.package.split('/')[1],'lib/client.js'));check(sha(b)===row.adaptedSha256,'UI_OUTPUT');write(rel,b);}
const preview='node_modules/@deepseek-ai/dsh-office-to-pdf/lib/index.js',officeAcq=sourcePins.find(p=>p.name==='@deepseek-ai/dsh-office-to-pdf');
const origin=fs.readFileSync(path.join('C:/Users/LI YUHAO/.codex/tmp/dsh020-r41-desktop/upstream-ui/dsh-office-to-pdf/lib/index.js'),'utf8');
check(sha(origin)===officeAcq.files['lib/index.js'],'OFFICE_UPSTREAM');
const seam='from "@deepseek-ai/libreoffice-kit"';check(origin.split(seam).length===2,'OFFICE_IMPORT');
const before=sha(fs.readFileSync(path.join(runtime,preview))),patched=origin.replace(seam,'from "../../../../office-remote/preview-converter.mjs"');check(before===sha(origin)||before===sha(patched),'OFFICE_RUNTIME');write(preview,patched);
// Company Office uses the existing shared conversion bridge. Never ship the
// unused native/WASM Office distribution or its remote postinstall scripts.
for(const name of fs.readdirSync(scoped).filter(n=>/^libreoffice-kit(?:-|$)/.test(n))){const target=fs.realpathSync(path.join(scoped,name));check(target.startsWith(fs.realpathSync(runtime)+path.sep),'PRUNE_OUTSIDE_CANDIDATE');fs.rmSync(target,{recursive:true});}
const oldOffice=json(path.join(prior,'office-remote/office-remote-manifest.json'));const bytes=Buffer.from(patched);const office={...oldOffice,preview:{dsh:version,upstreamSha256:sha(origin),seam:'createConverter import only'},files:{...oldOffice.files,[preview]:{bytes:bytes.length,sha256:sha(bytes)}}};writeJson('office-remote/office-remote-manifest.json',office);
const policy=json(path.join(app,'office-policy.json'));policy.remote.manifestSha256=sha(fs.readFileSync(path.join(runtime,'office-remote/office-remote-manifest.json')));fs.writeFileSync(path.join(app,'office-policy.json'),JSON.stringify(policy,null,2)+'\n');writeJson('office-remote/desktop-policy.json',policy);await require(path.join(app,'office-boundary.cjs')).verifyOfficeFull(runtime,{mode:'remote',policy});
const community=json(path.join(prior,'community-manifest.json'));for(const plugin of community.plugins){for(const [rel,pin] of Object.entries(plugin.files)){const b=fs.readFileSync(path.join(prior,'node_modules',plugin.name,rel));check(sha(b)===pin,'COMMUNITY_FILE');write('node_modules/'+plugin.name+'/'+rel,b);}const b=fs.readFileSync(path.join(prior,'node_modules',plugin.name,'megmeet-guarded-host.mjs'));check(sha(b)===oldRelease.community.plugins.find(x=>x.name===plugin.name).guardSha256,'COMMUNITY_GUARD');write('node_modules/'+plugin.name+'/megmeet-guarded-host.mjs',b);}
community.dsh=version;community.compatibility='DSH020_CANDIDATE_REAL_HOST_VERIFICATION_REQUIRED';writeJson('community-manifest.json',community);
// The executable browser adapter is separately sealed by the brand adaptation
// above. Retain the named-export authoring helper; never store a browser script
// referencing window under the helper's ES module filename.
const enterprise=fs.readFileSync(path.join(prior,'enterprise-native.mjs'));write('enterprise-native.mjs',enterprise);
const workspace=fs.readFileSync(path.join(repo,'output/dsh-rc2-implementation-20260928/ui/workspace-pinned-overlay.mjs'));write('workspace-pinned-overlay.mjs',workspace);
const brand={dsh:version,branding:oldBrand.branding,assets:oldBrand.assets,adaptations:candidate.adaptations,enterpriseAdapter:{version:'3.7.0',sha256:sha(enterprise)},workspaceAdapter:{version:'2.2.0',sha256:sha(workspace)}};writeJson('brand-adaptation.json',brand);
const resources={};
for(const key of Object.keys(packages)){const client=path.join(runtime,'node_modules',key,'lib/client.js');if(!fs.existsSync(client))continue;let source=fs.readFileSync(client,'utf8').replace(/(?:\r?\n)?\/\/# sourceURL=[^\r\n]*(?:\r?\n)?$/,'').replace(/(?:\r?\n)?\/\/# sourceMappingURL=[^\r\n]*(?:\r?\n)?$/,'');if(!source.endsWith('\n'))source+='\n';resources['/plugins/'+key+'/client.js']=sha(source+';\n');}
const dist=path.join(scoped,'dsh-web-frontend/dist');function assets(dir){for(const e of fs.readdirSync(path.join(dist,dir),{withFileTypes:true})){const rel=path.posix.join(dir,e.name);if(e.isDirectory())assets(rel);else if(/\.(?:js|css)$/.test(rel))resources['/'+rel]=sha(fs.readFileSync(path.join(dist,rel)));}}assets('assets');
const release={dsh:version,frontend:version,packages,resources,adaptations:brand.adaptations,branding:brand.branding,brandAssets:brand.assets,enterpriseAdapter:brand.enterpriseAdapter,workspaceAdapter:brand.workspaceAdapter,community:{...oldRelease.community,manifestSha256:sha(fs.readFileSync(path.join(runtime,'community-manifest.json'))),patchSha256:sha(fs.readFileSync(path.join(runtime,'community.patch.json')))}};release.releaseId=sha(JSON.stringify(release));writeJson('release-manifest.json',release);
const nodeSha='d14ba95cdce1ef7dc9ad3ac74949ca5db38b27378ee30f30a23cf26f9e875a11';check(sha(fs.readFileSync(path.join(runtime,'bin/node.exe')))===nodeSha,'NODE');
writeJson('runtime-manifest.json',{dsh:version,platform:'win32-x64',node:'24.13.0',nodeSha256:nodeSha,npmLockSha256:sha(fs.readFileSync(path.join(runtime,'package-lock.json'))),source:'MEGMEET 0.13.0 / DSH0.2.0-rc.2 integration r41',releaseSha256:sha(fs.readFileSync(path.join(runtime,'release-manifest.json'))),adaptationSha256:sha(fs.readFileSync(path.join(runtime,'brand-adaptation.json')))});
const result={status:'ASSEMBLED_ADAPTER_TESTS_PENDING',dsh:version,packages,packageSetSha256:sha(JSON.stringify(Object.keys(packages).sort().map(k=>[k,packages[k]]))),lockedDshPackageVersionDistribution:lockDistribution,nestedLockPinsVerified:true,companyRequests:0};
fs.writeFileSync(path.join(root,'evidence/runtime-assembly.json'),JSON.stringify(result,null,2)+'\n');fs.copyFileSync(path.join(runtime,'package-lock.json'),path.join(root,'runtime-package-lock.json'));
console.log(JSON.stringify({status:result.status,packageCount:Object.keys(packages).length,packageSetSha256:result.packageSetSha256}));
