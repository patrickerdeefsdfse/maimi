import {mkdir,readFile,lstat,writeFile,rename,copyFile} from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';

// The caller validates its runtime release before invoking this offline step.
// Keep fixed plugin code/auth guards in the runtime, writable preferences in
// the native profile; CLI insert/config overlays cannot be edited by 0.1.7.
export async function prepareCommunityProfile({home,runtimeRoot,yaml,profile='web'}) {
  const defaults={
    'dshp-file-change-viewer':{directory:'file-change-viewer',config:{patchTool:false}},
    'dshp-web-style':{directory:'web-style',config:{themeId:'',radius:{global:-1}}},
    'dshp-widget-kit':{directory:'widget-kit',config:{referenceWidgets:false}},
    'dshp-token-meter':{directory:'token-meter',config:{vendors:[],activeVendor:''}},
  };
  if(profile!=='web')throw Error('COMMUNITY_PROFILE_NOT_REVIEWED');
  const dir=path.join(home,'profiles',profile), file=path.join(dir,'cordis.patch.yml');
  await mkdir(dir,{recursive:true});
  let text;
  try{const info=await lstat(file);if(!info.isFile()||info.isSymbolicLink()||info.size>1024*1024)throw Error('COMMUNITY_PROFILE_INVALID');text=await readFile(file,'utf8');}
  catch(error){if(error.code!=='ENOENT')throw error;}
  const doc=yaml.parseDocument(text??'[]\n',{customTags:[{tag:'tag:yaml.org,2002:js',resolve:value=>value}]});
  if(doc.errors.length||!yaml.isSeq(doc.contents))throw Error('COMMUNITY_PROFILE_INVALID');
  const preferences=new Map(), disabled=new Map(), inserts=new Set(), rows=[];
  const managed=row=>yaml.isMap(row)&&Object.hasOwn(defaults,row.get('id'));
  const remember=row=>{
    if(row.has('disabled')){const value=row.get('disabled');if(typeof value!=='boolean')throw Error('COMMUNITY_DISABLED_INVALID');disabled.set(row.get('id'),value);}
    const config=row.get('config',true);
    if(config!==undefined){if(!yaml.isMap(config))throw Error('COMMUNITY_CONFIG_INVALID');preferences.set(row.get('id'),config.clone());}
  };
  for(const row of doc.contents.items){
    if(!yaml.isMap(row)){rows.push(row);continue;}
    if(managed(row)){remember(row);continue;}
    const insert=row.get('insert',true);
    if(insert!==undefined){
      if(!yaml.isSeq(insert))throw Error('COMMUNITY_INSERT_INVALID');
      insert.items=insert.items.filter(item=>{
        if(!managed(item))return true;
        const id=item.get('id');if(inserts.has(id))throw Error('COMMUNITY_DUPLICATE_ENTRY');inserts.add(id);remember(item);return false;
      });
      if(!insert.items.length)row.delete('insert');
    }
    if(row.items.length)rows.push(row);
  }
  doc.contents.items=rows;
  const entries=[];
  for(const [id,definition] of Object.entries(defaults)){
    const config=preferences.get(id)??doc.createNode(definition.config);
    for(const [key,value] of Object.entries(definition.config))if(!config.has(key))config.set(key,doc.createNode(value));
    const entry=doc.createNode({id,name:pathToFileURL(path.join(runtimeRoot,'node_modules/@dshp',definition.directory,'megmeet-guarded-host.mjs')).href});
    if(disabled.has(id))entry.set('disabled',disabled.get(id));
    entry.set('config',config);entries.push(entry);
  }
  const insertion=doc.createNode({insert:[]});insertion.get('insert',true).items=entries;doc.add(insertion);
  const next=String(doc);
  if(next===text)return {changed:false};
  if(text!==undefined){try{await copyFile(file,file+'.before-megmeet-community17',constants.COPYFILE_EXCL);}catch(error){if(error.code!=='EEXIST')throw error;}}
  const temporary=file+'.megmeet-community-'+randomUUID()+'.tmp';
  await writeFile(temporary,next,{flag:'wx',mode:0o600});await rename(temporary,file);
  return {changed:true};
}
