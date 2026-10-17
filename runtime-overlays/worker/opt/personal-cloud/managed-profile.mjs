import { mkdir, readFile, rename, writeFile, copyFile, lstat } from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { randomUUID } from 'node:crypto';

// The current DSH settings editor writes profile config, and deliberately
// refuses changes hidden by launch overlays. Seed just our managed entries in
// that writable profile layer; leave employee UI and tool preferences intact.
export async function prepareManagedProfile({home, profile='web', catalogConfig, defaultConfig, yaml}) {
  const directory=path.join(home,'profiles',profile);
  await mkdir(directory,{recursive:true});
  const boundedRead=async file=>{
    try { const info=await lstat(file); if(!info.isFile()||info.isSymbolicLink()||info.size>1024*1024)throw Error('cloud profile file invalid'); return await readFile(file,'utf8'); }
    catch(error){if(error.code==='ENOENT')return null;throw error;}
  };
  const atomic=async(file,content)=>{
    const temporary=file+'.megmeet-'+randomUUID()+'.tmp';
    await writeFile(temporary,content,{mode:0o600,flag:'wx'});
    await rename(temporary,file);
  };
  const backupOnce=async file=>{
    try{await copyFile(file,file+'.before-megmeet-dsh17',constants.COPYFILE_EXCL);}
    catch(error){if(error.code!=='EEXIST')throw error;}
  };
  const manifest=path.join(directory,'package.json');
  if(await boundedRead(manifest)===null)await atomic(manifest,JSON.stringify({name:'dsh-profile-'+profile,private:true,dependencies:{},dsh:{profile:{bundles:['@deepseek-ai/dsh-base','@deepseek-ai/dsh-web-app']}}})+'\n');
  const patchFile=path.join(directory,'cordis.patch.yml');
  const text=await boundedRead(patchFile);
  const document=yaml.parseDocument(text??'[]\n',{customTags:[{tag:'tag:yaml.org,2002:js',resolve:value=>value}]});
  if(document.errors.length||!yaml.isSeq(document.contents))throw Error('cloud profile patch invalid');
  const managed=new Set(['agent-default-model','llm-pi-ai']);
  document.contents.items=document.contents.items.filter(row=>!yaml.isMap(row)||!managed.has(row.get('id')));
  document.add(document.createNode({id:'agent-default-model',config:defaultConfig}));
  document.add(document.createNode({id:'llm-pi-ai',config:catalogConfig}));
  if(text!==null)await backupOnce(patchFile);
  await atomic(patchFile,String(document));
  // DSH imports this old document asynchronously. Preserve its exact bytes,
  // then let native DSH import all non-model preferences without racing today's
  // authoritative catalog/default with a former employee lease's settings.
  const legacyFile=path.join(home,'settings.yaml');
  const legacyText=await boundedRead(legacyFile);
  if(legacyText!==null){
    const legacy=yaml.parseDocument(legacyText);
    if(legacy.errors.length||!yaml.isMap(legacy.contents))throw Error('cloud legacy settings invalid');
    const removed=['llm-deepseek','llm-pi-ai','agent-default-model'].filter(key=>legacy.has(key));
    if(removed.length){await backupOnce(legacyFile);for(const key of removed)legacy.delete(key);await atomic(legacyFile,String(legacy));}
  }
}
