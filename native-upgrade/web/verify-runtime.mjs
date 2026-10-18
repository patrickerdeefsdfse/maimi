import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';

const VERSION='0.2.1-rc.2';
const REQUIRED_PACKAGES=['dsh','dsh-api-gateway','dsh-host-webserver','dsh-session','dsh-api-workspace-controller','dsh-client-ui-sidebar','dsh-client-ui-chat','libreoffice-kit','libreoffice-kit-wasm'];
const REQUIRED_COMPANY=['/opt/dsh/package.json','/opt/dsh/package-lock.json','/opt/dsh/plugins/company-ui/index.mjs','/opt/dsh/plugins/company-ui/client.js','/opt/dsh/plugins/company-ui/package.json','/opt/personal-cloud/worker.mjs','/opt/personal-cloud/task-authority.mjs','/opt/personal-cloud/enterprise-profile.mjs','/opt/personal-cloud/community-profile.mjs','/opt/personal-cloud/company-workspace-controller.mjs'];
const hash=data=>createHash('sha256').update(data).digest('hex');
const digest=value=>typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
const object=value=>value&&typeof value==='object'&&!Array.isArray(value);
const safeRelative=value=>typeof value==='string'&&value.length>0&&!value.includes('\\')&&!path.posix.isAbsolute(value)&&value.split('/').every(x=>x&&x!=='.'&&x!=='..');
function inside(root,file){const rel=path.relative(root,file);return rel!==''&&!rel.startsWith('..'+path.sep)&&rel!=='..'&&!path.isAbsolute(rel);}

export function verify(root, expectedManifestHash){
 root=path.resolve(root);
 const raw=fs.readFileSync(path.join(root,'candidate-manifest.json'));
 if(expectedManifestHash&&(!digest(expectedManifestHash)||hash(raw)!==expectedManifestHash))throw Error('MANIFEST_DIGEST_MISMATCH');
 const manifest=JSON.parse(raw);
 if(manifest.schema!=='maimi-native021-runtime-candidate-v1'||manifest.runtime!==VERSION||manifest.official_packages_modified!==0||manifest.modules_mode!=='COPIED_COMPLETE_TREE')throw Error('INVALID_RUNTIME_MANIFEST');
 for(const key of ['packages','company_source_targets','module_files'])if(!object(manifest[key])||!Object.keys(manifest[key]).length)throw Error('EMPTY_REQUIRED_INVENTORY');
 for(const name of REQUIRED_PACKAGES)if(!Object.hasOwn(manifest.packages,'@deepseek-ai/'+name))throw Error('MISSING_REQUIRED_PACKAGE');
 for(const name of REQUIRED_COMPANY)if(!Object.hasOwn(manifest.company_source_targets,name))throw Error('MISSING_REQUIRED_COMPANY_SOURCE');
 if(!digest(manifest.source_package_lock_sha256)||hash(fs.readFileSync(path.join(root,'opt/dsh/package-lock.json')))!==manifest.source_package_lock_sha256)throw Error('SOURCE_LOCK_MISMATCH');
 const modules=path.join(root,'opt/dsh/node_modules');
 if(fs.lstatSync(modules).isSymbolicLink())throw Error('MODULE_ROOT_LINK');
 const found=new Set();
 function visit(directory){
  for(const item of fs.readdirSync(directory,{withFileTypes:true})){
   const file=path.join(directory,item.name),rel=path.relative(modules,file).split(path.sep).join('/');
   if(item.isDirectory()){visit(file);continue;}
   const expected=manifest.module_files[rel];
   if(!safeRelative(rel)||!object(expected))throw Error('UNEXPECTED_MODULE_FILE');
   found.add(rel);
   if(item.isSymbolicLink()){
    if(expected.kind!=='symlink'||fs.readlinkSync(file)!==expected.target||!inside(modules,fs.realpathSync(file)))throw Error('MODULE_LINK_MISMATCH');
   }else if(!item.isFile()||expected.kind!=='file'||!digest(expected.sha256)||hash(fs.readFileSync(file))!==expected.sha256)throw Error('MODULE_CONTENT_MISMATCH');
  }
 }
 visit(modules);
 if(found.size!==Object.keys(manifest.module_files).length)throw Error('MISSING_MODULE_FILE');
 for(const [name,version] of Object.entries(manifest.packages)){
  if(!/^@deepseek-ai\/[A-Za-z0-9._-]+$/.test(name))throw Error('INVALID_PACKAGE_NAME');
  if((name==='@deepseek-ai/dsh'||name.startsWith('@deepseek-ai/dsh-'))&&version!==VERSION)throw Error('MIXED_DSH_RELEASE');
  const actual=JSON.parse(fs.readFileSync(path.join(modules,name,'package.json'),'utf8'));
  if(actual.name!==name||actual.version!==version)throw Error('PACKAGE_VERSION_MISMATCH');
 }
 for(const [name,sha] of Object.entries(manifest.company_source_targets)){
  if(!/^\/opt\/(?:dsh|personal-cloud)\//.test(name)||!safeRelative(name.slice(1))||!digest(sha))throw Error('INVALID_COMPANY_TARGET');
  const file=path.join(root,name),info=fs.lstatSync(file);
  if(!info.isFile()||info.isSymbolicLink()||!inside(root,fs.realpathSync(file))||hash(fs.readFileSync(file))!==sha)throw Error('COMPANY_SOURCE_MISMATCH');
 }
 return {status:'VERIFIED',runtime:manifest.runtime,installed_module_entries:found.size,module_content_matches_assembly:true,source_package_lock_sha256:manifest.source_package_lock_sha256,manifest_sha256:hash(raw),company_source_targets:Object.keys(manifest.company_source_targets).length,company_services_changed:false};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href)console.log(JSON.stringify(verify(process.argv[2]??'/',process.argv[3])));

