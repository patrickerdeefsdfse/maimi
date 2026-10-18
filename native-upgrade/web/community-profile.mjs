import {readFile,lstat,writeFile,rename,copyFile} from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';

// Older adapters targeted private UI internals. Retire only their known rows;
// retain a byte-exact backup and every unrelated employee preference.
const retired=new Set(['dshp-file-change-viewer','dshp-web-style','dshp-widget-kit','dshp-token-meter']);
export async function prepareCommunityProfile({home,yaml,profile='web'}) {
  if(profile!=='web') throw Error('COMMUNITY_PROFILE_NOT_REVIEWED');
  const file=path.join(home,'profiles',profile,'cordis.patch.yml');
  let text;
  try {
    const info=await lstat(file);
    if(!info.isFile()||info.isSymbolicLink()||info.size>1024*1024) throw Error('COMMUNITY_PROFILE_INVALID');
    text=await readFile(file,'utf8');
  } catch(error) { if(error.code==='ENOENT') return {changed:false}; throw error; }
  const doc=yaml.parseDocument(text,{customTags:[{tag:'tag:yaml.org,2002:js',resolve:value=>value}]});
  if(doc.errors.length||!yaml.isSeq(doc.contents)) throw Error('COMMUNITY_PROFILE_INVALID');
  let removed=0;
  doc.contents.items=doc.contents.items.filter(row=>{
    if(!yaml.isMap(row))return true;
    if(retired.has(row.get('id'))){removed++;return false;}
    const insert=row.get('insert',true);
    if(insert!==undefined){
      if(!yaml.isSeq(insert))throw Error('COMMUNITY_INSERT_INVALID');
      insert.items=insert.items.filter(item=>{if(yaml.isMap(item)&&retired.has(item.get('id'))){removed++;return false;}return true;});
      if(!insert.items.length)row.delete('insert');
    }
    return row.items.length>0;
  });
  if(!removed)return {changed:false};
  try{await copyFile(file,file+'.before-megmeet-native021',constants.COPYFILE_EXCL);}
  catch(error){if(error.code!=='EEXIST')throw error;}
  const temporary=file+'.megmeet-native021-'+randomUUID()+'.tmp';
  await writeFile(temporary,String(doc),{flag:'wx',mode:0o600});
  await rename(temporary,file);
  return {changed:true,retiredRows:removed};
}
