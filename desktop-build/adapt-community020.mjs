import fs from 'node:fs';import path from 'node:path';import {createHash} from 'node:crypto';import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),repo=path.resolve(root,'../../..'),runtime='C:/Users/LI YUHAO/.codex/tmp/dsh020-r41-desktop/runtime-win32-x64';
const original=path.join(repo,'output/dsh-rc2-implementation-20260928/ui/runtime-win32-x64/node_modules/@dshp');
const sha=b=>createHash('sha256').update(b).digest('hex'),j=p=>JSON.parse(fs.readFileSync(p));
const primitives=fs.readFileSync(path.join(runtime,'node_modules/@deepseek-ai/dsh-client-ui-primitives/lib/index.js'),'utf8');
const exported=primitives.match(/export \{([^}]+)\};?\s*(?:\/\/[^\n]*\n?)*$/)?.[1];if(!exported)throw Error('PRIMITIVES_EXPORTS_UNREADABLE');
const names=new Set(exported.split(',').map(x=>x.trim().split(/\s+as\s+/).at(-1)));
const adaptations=[];
for(const directory of ['file-change-viewer','web-style','widget-kit','token-meter']){
 const src=path.join(original,directory),dest=path.join(root,'overlays/@dshp',directory),before=fs.readFileSync(path.join(src,'lib/client.js'));
 let after=before.toString('utf8');const references=[...new Set([...after.matchAll(/import_dsh_client_ui_primitives\d*\.([A-Za-z0-9_]+)/g)].map(m=>m[1]))],transformations={};
 for(const symbol of references){if(names.has(symbol))continue;const mapped=symbol.replace(/(Outline|Filled)\d+$/,'$1Regular');if(mapped===symbol||!names.has(mapped))throw Error('COMMUNITY_API_NOT_MAPPED:'+directory+':'+symbol);transformations[symbol]=mapped;after=after.replaceAll(symbol,mapped);}
 const packageBefore=fs.readFileSync(path.join(src,'package.json')),pkg=JSON.parse(packageBefore);
 if(pkg.peerDependencies?.['@deepseek-ai/dsh-client-ui-primitives'])pkg.peerDependencies['@deepseek-ai/dsh-client-ui-primitives']='0.2.0-rc.2';
 pkg.megmeetReviewedRuntime='0.2.0-rc.2';pkg.megmeetAdaptation='community-ui-020-r41';
 const packageAfter=JSON.stringify(pkg,null,2)+'\n';fs.mkdirSync(path.join(dest,'lib'),{recursive:true});fs.writeFileSync(path.join(dest,'lib/client.js'),after);fs.writeFileSync(path.join(dest,'package.json'),packageAfter);
 const changes={'lib/client.js':{originalSha256:sha(before),sha256:sha(after)},'package.json':{originalSha256:sha(packageBefore),sha256:sha(packageAfter)}};
 for(const rel of Object.keys(changes))fs.copyFileSync(path.join(dest,rel),path.join(runtime,'node_modules/@dshp',directory,rel));
 adaptations.push({package:pkg.name,version:pkg.version,runtime:'0.2.0-rc.2',primitivesReferences:references,transformations,files:changes});
}
const community=j(path.join(runtime,'community-manifest.json'));
for(const item of adaptations)for(const [rel,v] of Object.entries(item.files))community.plugins.find(p=>p.name===item.package).files[rel]=v.sha256;
community.megmeetAdaptations=adaptations;fs.writeFileSync(path.join(runtime,'community-manifest.json'),JSON.stringify(community,null,2)+'\n');
const release=j(path.join(runtime,'release-manifest.json'));release.community.manifestSha256=sha(fs.readFileSync(path.join(runtime,'community-manifest.json')));delete release.releaseId;release.releaseId=sha(JSON.stringify(release));fs.writeFileSync(path.join(runtime,'release-manifest.json'),JSON.stringify(release,null,2)+'\n');
const manifest=j(path.join(runtime,'runtime-manifest.json'));manifest.releaseSha256=sha(fs.readFileSync(path.join(runtime,'release-manifest.json')));fs.writeFileSync(path.join(runtime,'runtime-manifest.json'),JSON.stringify(manifest,null,2)+'\n');
fs.writeFileSync(path.join(root,'evidence/community-adaptation.json'),JSON.stringify({status:'ADAPTED_REAL_UI_TEST_PENDING',adaptations,profileExemptionsWritten:false,upstreamGuardsUnchanged:true},null,2)+'\n');
console.log(JSON.stringify(adaptations.map(a=>({package:a.package,transformations:a.transformations,allPrimitivesReferencesChecked:a.primitivesReferences.length}))));
