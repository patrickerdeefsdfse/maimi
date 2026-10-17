import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
const source=path.dirname(fileURLToPath(import.meta.url)), base=path.dirname(source);
const runtime=path.resolve(process.argv[2]||path.join(base,'desktop-local/runtime'));
const target=path.join(runtime,'toolkit');
const sha=b=>createHash('sha256').update(b).digest('hex');
const files=['session-tools.mjs','http.mjs','desktop.mjs','browser.mjs','windows.py','adapter/index.mjs','package.json','package-lock.json'];
const dependency=JSON.parse(await fs.readFile(path.join(source,'node_modules/playwright-core/package.json')));
if(dependency.version!=='1.63.0')throw Error('TOOLKIT_DEPENDENCY_VERSION_MISMATCH');
await fs.mkdir(target,{recursive:true});
for(const name of files){await fs.mkdir(path.dirname(path.join(target,name)),{recursive:true});await fs.copyFile(path.join(source,name),path.join(target,name));}
await fs.cp(path.join(source,'node_modules/playwright-core'),path.join(target,'node_modules/playwright-core'),{recursive:true,force:true});
async function walk(dir,prefix=''){
  const out={};
  for(const e of await fs.readdir(dir,{withFileTypes:true})){
    const name=prefix+e.name;if(!prefix&&e.name==='toolkit-manifest.json')continue;
    if(e.isSymbolicLink())throw Error('TOOLKIT_SYMLINK_REFUSED');
    if(e.isDirectory())Object.assign(out,await walk(path.join(dir,e.name),name+'/'));
    else{const b=await fs.readFile(path.join(dir,e.name));out[name]={bytes:b.length,sha256:sha(b)};}
  }return out;
}
const manifest={protocol:'megmeet-connectors-v1',version:'1.0.0',dependencies:{'playwright-core':dependency.version},files:await walk(target)};
const bytes=Buffer.from(JSON.stringify(manifest,null,2)+'\n');
await fs.writeFile(path.join(target,'toolkit-manifest.json'),bytes);
await fs.writeFile(path.join(base,'desktop-local/toolkit-policy.json'),JSON.stringify({version:manifest.version,manifestSha256:sha(bytes)},null,2)+'\n');
console.log('TOOLKIT_STAGED:',Object.keys(manifest.files).length,'files; protocol',manifest.protocol);
