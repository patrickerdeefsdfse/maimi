/** Reproducible enterprise derivatives. Never rewrites downloaded npm packages. */
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {VERSIONS} from './policy.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
const sha=b=>createHash('sha256').update(b).digest('hex');
const HASHES={
 'dsh-context/lib/index.js':'66e2d6d5cbf17a7dd1ef63a8b6c8ad016e1bf6035ae0b023917de9ad411dbf08',
 'dsh-context/lib/client.js':'df0f7123323e422da108a5bbed299642b51032b05fb8d5402f21c75990205ce7',
 'dsh-better-sidebar/lib/client.js':'53b70b1b5317205247c074b8121e1ececec1df2dc3771b0641f0a66b8fd127e1',
 '@changfenhuang/dsh-genui/lib/index.js':'da9e50d494e710b4e675aa2a342a2776ff2e8c2bba80a5a336d08412b12cb201',
 '@changfenhuang/dsh-genui/lib/client.js':'3e6c1db51e3c178c0b191f14350c478c336f7c59fc239ede3edcd009c481bcba',
 '@wxg-prc-cpg/browser-skill-dsh-plugin/lib/index.mjs':'9e08c2a365aeced42ee829a37275945b0cea3efc012170579932ee7060f235e2',
 'dsh-files/lib/tool.js':'6f8ec3aece2c6a48e0f9e4ce8e17b52c53bbaac4b135993440df5afd0b66abe0',
};
function replaceOnce(source,from,to){if(source.split(from).length!==2)throw Error('MANAGED_PATCH_CONTEXT_CHANGED');return source.replace(from,to);}
export function buildManaged({runtimeRoot,out}) {
 if(!path.isAbsolute(runtimeRoot)||!path.isAbsolute(out))throw Error('ABSOLUTE_MANAGED_BUILD_PATH_REQUIRED');
 if(fs.existsSync(out))throw Error('MANAGED_OUTPUT_MUST_BE_NEW');
 const read=(relative)=>{const data=fs.readFileSync(path.join(runtimeRoot,'node_modules',relative));if(HASHES[relative]&&sha(data)!==HASHES[relative])throw Error('UPSTREAM_PLUGIN_HASH_MISMATCH:'+relative);return data;};
 for(const [pkg,version] of [['@deepseek-ai/dsh',VERSIONS.dsh],['dsh-context',VERSIONS.context],['dsh-better-sidebar',VERSIONS.sidebar]]){
  if(JSON.parse(read(pkg+'/package.json')).version!==version)throw Error('MANAGED_PLUGIN_VERSION_MISMATCH:'+pkg);
 }
 let host=read('dsh-context/lib/index.js').toString('utf8');
 host=replaceOnce(host,'or(e),fi(e),ei(e,n),Rn(e,n)','fi(e),ei(e,n),Rn(e,n)');
 host=replaceOnce(host,'hr(e,n),ii(e,t)','hr(e,n)');
 // Return the canonical attachment path. The upstream image-preview helper
 // otherwise creates hardlinks or copies outside the attachment quota ledger.
 const imageStart=host.indexOf('async function In(e,t){'), imageEnd=host.indexOf('async function Ln(',imageStart);
 if(imageStart<0||imageEnd<imageStart)throw Error('ATTACHMENT_PATCH_CONTEXT_CHANGED');
 host=host.slice(0,imageStart)+'async function In(e,t){if(typeof e?.imageHostPath!==`function`)return null;try{let n=e.imageHostPath(t);return typeof n===`string`&&n!==``?n:null}catch{return null}}'+host.slice(imageEnd);
 let client=read('dsh-context/lib/client.js').toString('utf8');
 client=replaceOnce(client,'let Fo=()=>Mo().providers()','let Fo=async()=>null');
 client=replaceOnce(client,'zo=setTimeout(()=>{zo=null,Bo.size!==0&&Ho()},3e4*2**Math.min(Ro-1,3))','zo=null');
 fs.mkdirSync(out,{recursive:true});const files=[];
 const put=(relative,bytes)=>{const target=path.join(out,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,bytes,{flag:'wx'});files.push({path:relative,bytes:Buffer.byteLength(bytes),sha256:sha(bytes)});};
 for(const file of ['index.mjs','policy.mjs','client.js'])put(file,fs.readFileSync(path.join(here,file)));
 const packageFor=(name,version)=>{const original=JSON.parse(read(name+'/package.json'));return {name,version,type:'module',private:true,license:original.license,main:'index.mjs',exports:{'.':'./index.mjs','./client':'./client.js','./package.json':'./package.json','./locale/*.json':'./locale/*.json'},engines:{dsh:VERSIONS.dsh},dsh:{client:original.dsh.client},maimi:{upstreamPackage:name,upstreamVersion:version,enterpriseDerivative:true}};};
 for(const [sub,pkg,version] of [['context','dsh-context',VERSIONS.context],['sidebar','dsh-better-sidebar',VERSIONS.sidebar]]){
  put(sub+'/index.mjs',fs.readFileSync(path.join(here,sub,'index.mjs')));
  put(sub+'/package.json',JSON.stringify(packageFor(pkg,version),null,2)+'\n');
  for(const file of ['LICENSE','LICENSE.md']){const p=path.join(runtimeRoot,'node_modules',pkg,file);if(fs.existsSync(p))put(sub+'/'+file,fs.readFileSync(p));}
  const locale=path.join(runtimeRoot,'node_modules',pkg,'locale');
  if(fs.existsSync(locale))for(const item of fs.readdirSync(locale,{withFileTypes:true}))if(item.isFile())put(sub+'/locale/'+item.name,fs.readFileSync(path.join(locale,item.name)));
 }
 put('context/upstream.mjs',host);put('context/client.js',client);
 const relative=path.relative(path.join(out,'sidebar'),path.join(runtimeRoot,'node_modules/dsh-better-sidebar/lib/index.js')).split(path.sep).join('/');
 put('sidebar/upstream.mjs',`export * from ${JSON.stringify(relative.startsWith('.')?relative:'./'+relative)};\n`);
 put('sidebar/client.js',read('dsh-better-sidebar/lib/client.js'));
 // Re-export the pinned host implementation without rewriting immutable npm
 // bytes. The enterprise wrapper controls what is registered and where it runs.
 const bridge=(sub,pkg,file)=>{
  read(pkg+'/'+file);const relative=path.relative(path.join(out,sub),path.join(runtimeRoot,'node_modules',pkg,file)).split(path.sep).join('/');
  put(sub+'/upstream.mjs',`export * from ${JSON.stringify(relative.startsWith('.')?relative:'./'+relative)};\n`);
 };
 for(const [sub,pkg,version,file] of [['files','dsh-files',VERSIONS.files,'lib/tool.js'],['genui','@changfenhuang/dsh-genui',VERSIONS.genui,'lib/index.js'],['browser','@wxg-prc-cpg/browser-skill-dsh-plugin',VERSIONS.browser,'lib/index.mjs']]){
  if(JSON.parse(read(pkg+'/package.json')).version!==version)throw Error('MANAGED_PLUGIN_VERSION_MISMATCH:'+pkg);
  put(sub+'/index.mjs',fs.readFileSync(path.join(here,sub,'index.mjs')));bridge(sub,pkg,file);
  const metadata=packageFor(pkg,version);
  if(sub!=='genui'){delete metadata.dsh;delete metadata.exports['./client'];metadata.name=sub==='files'?'maimi-documents':'maimi-browser-skill';}
  put(sub+'/package.json',JSON.stringify(metadata,null,2)+'\n');
  for(const name of ['LICENSE','LICENSE.md','THIRD_PARTY_NOTICES.md']){const file=path.join(runtimeRoot,'node_modules',pkg,name);if(fs.existsSync(file))put(sub+'/'+name,fs.readFileSync(file));}
 }
 let genui=read('@changfenhuang/dsh-genui/lib/client.js').toString('utf8');
 if(genui.includes('window.localStorage')||genui.includes('globalThis.localStorage')||genui.split('localStorage').length!==11)throw Error('GENUI_STORAGE_PATCH_CONTEXT_CHANGED');
 genui=genui.replaceAll('localStorage','__maimiMemoryStorage');
 genui=replaceOnce(genui,'factory:e=>{','factory:e=>{const __maimiMemoryValues=new Map();const __maimiMemoryStorage={getItem:k=>__maimiMemoryValues.get(k)??null,setItem:(k,v)=>{if(String(v).length<=1048576){if(__maimiMemoryValues.size>=32&&!__maimiMemoryValues.has(k))__maimiMemoryValues.delete(__maimiMemoryValues.keys().next().value);__maimiMemoryValues.set(k,String(v));}},removeItem:k=>__maimiMemoryValues.delete(k)};');
 const mediaStart=genui.indexOf('function fo(e){'),mediaEnd=genui.indexOf('function po(',mediaStart);
 if(mediaStart<0||mediaEnd<mediaStart)throw Error('GENUI_MEDIA_PATCH_CONTEXT_CHANGED');
 genui=genui.slice(0,mediaStart)+'function fo(e){if(typeof e!==`string`||e.length>2048)return;try{const u=new URL(e,document.baseURI);if(u.origin===location.origin&&/^https?:$/.test(u.protocol))return e;}catch{}}'+genui.slice(mediaEnd);
 put('genui/client.js',genui);
 for(const file of ['index.mjs','client.js'])put('appearance/'+file,fs.readFileSync(path.join(here,'appearance',file)));
 put('appearance/package.json',JSON.stringify({name:'maimi-appearance',version:'0.0.0-r73',private:true,type:'module',main:'index.mjs',exports:{'.':'./index.mjs','./client':'./client.js','./package.json':'./package.json'},engines:{dsh:VERSIONS.dsh},dsh:{client:{platform:'web',inject:['@deepseek-ai/dsh-client-ui-slots']}}})+'\n');
 put('package.json',JSON.stringify({name:'maimi-managed-plugins',version:'0.0.0-r73',private:true,type:'module',main:'index.mjs',exports:{'.':'./index.mjs','./client':'./client.js','./package.json':'./package.json'},engines:{dsh:VERSIONS.dsh},dsh:{client:{platform:'web',inject:['@deepseek-ai/dsh-client-ui-slots','@deepseek-ai/dsh-client-ui-deliverables','@deepseek-ai/dsh-client-ui-conversation']}}})+'\n');
 const receipt={schema:'maimi-r73-managed-plugin-build/v1',runtime:VERSIONS.dsh,sourceHashes:HASHES,files,officialDshPackagesModified:false,originalNpmPluginsModified:false,disabled:['context-global-prompt-injection','context-public-balance','context-public-model-prices','context-duplicate-preview-cache','dsh-files-attachment-library','genui-durable-browser-business-state','genui-external-media','genui-automatic-model-repair','background-media-storage','unpaired-browser-execution'],serverDeployment:false};
 fs.writeFileSync(path.join(out,'build-receipt.json'),JSON.stringify(receipt,null,2)+'\n',{flag:'wx'});
 return receipt;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){const args=process.argv.slice(2);const value=key=>args[args.indexOf(key)+1];const result=buildManaged({runtimeRoot:value('--runtime'),out:value('--out')});console.log(JSON.stringify({status:'BUILT',runtime:result.runtime,files:result.files.length}));}
