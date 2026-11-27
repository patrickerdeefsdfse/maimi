import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {fileURLToPath} from 'node:url';

export const name='maimi-wps-read';
export const inject=['skills'];
const source=fileURLToPath(new URL('./',import.meta.url));
const files=['SKILL.md','read-wps.mjs','read-wps.ps1'];
function directoryWithoutLinks(directory){
  const resolved=path.resolve(directory), parsed=path.parse(resolved);
  let current=parsed.root;
  for(const part of resolved.slice(parsed.root.length).split(path.sep).filter(Boolean)){
    current=path.join(current,part);
    try{const info=fs.lstatSync(current);if(info.isSymbolicLink()||!info.isDirectory())throw Error('WPS_RESOURCE_LINK_REFUSED');}
    catch(error){if(error.code!=='ENOENT')throw error;fs.mkdirSync(current,{mode:0o700});}
  }
}
export function installResources(root){
  if(!path.isAbsolute(root))throw Error('WPS_RESOURCE_ROOT_REQUIRED');
  const payloads=files.map(name=>[name,fs.readFileSync(path.join(source,name))]);
  const hash=crypto.createHash('sha256');for(const [name,data]of payloads)hash.update(name+'\0').update(data);
  const directory=path.join(root,'r102-'+hash.digest('hex').slice(0,20));
  directoryWithoutLinks(directory);
  if(fs.lstatSync(directory).isSymbolicLink())throw Error('WPS_RESOURCE_LINK_REFUSED');
  for(const [name,data]of payloads){
    const target=path.join(directory,name);
    if(fs.existsSync(target)){
      if(!fs.lstatSync(target).isFile()||fs.lstatSync(target).isSymbolicLink()||!fs.readFileSync(target).equals(data))throw Error('WPS_RESOURCE_CHANGED');
    }else fs.writeFileSync(target,data,{flag:'wx',mode:0o600});
  }
  return directory;
}
export function apply(ctx,{root,node,platform=process.platform}){
  if(platform!=='win32')return;
  if(!path.isAbsolute(node)||!fs.statSync(node).isFile())throw Error('WPS_NODE_REQUIRED');
  const directory=installResources(root);
  const raw=fs.readFileSync(path.join(directory,'SKILL.md'),'utf8');
  const match=/^---\r?\nname: wps-read\r?\ndescription: ([^\r\n]+)\r?\n---\r?\n/u.exec(raw);
  if(!match)throw Error('WPS_SKILL_HEADER_INVALID');
  ctx.skills.register({name:'wps-read',description:match[1],source:'bundled',provider:'maimi-wps',
    invocation:{modelInvocable:true,userInvocable:false},resourceBase:{kind:'directory',path:directory},
    content:raw.slice(match[0].length)+'\n\n## 本机调用位置\n\n'+JSON.stringify({node,reader:path.join(directory,'read-wps.mjs')})});
}
