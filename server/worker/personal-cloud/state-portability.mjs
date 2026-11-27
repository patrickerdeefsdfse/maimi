// Read-only portability proof while idle-drained. Unknown local data blocks handoff.
import {readFile,writeFile,lstat,readdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {join} from 'node:path';
const digest=raw=>createHash('sha256').update(raw).digest('hex');
const fail=code=>{throw Error(code);};
async function kind(path){try{const s=await lstat(path);if(s.isSymbolicLink())fail('LOCAL_STATE_SYMLINK');return s;}catch(e){if(e.code==='ENOENT')return null;throw e;}}
async function bounded(path){const s=await kind(path);if(!s)return null;if(!s.isFile()||s.size>262144)fail('LOCAL_STATE_FILE_UNQUALIFIED');return readFile(path);}
export async function createPortability({enabled,firstUse,root='/state',owner}){
 if(!enabled)return {finishStartup:async()=>{},proof:async()=>fail('LOCAL_STATE_RELOCATION_DISABLED')};
 if(!/^[a-f0-9]{32}$/.test(owner))fail('LOCAL_STATE_OWNER_INVALID');
 const marker=join(root,'.maimi-state-portability.json'),profile=join(root,'dsh/profiles/web/cordis.patch.yml');
 const raw=await bounded(marker);let saved=raw?JSON.parse(raw):null;
 if(saved){if(saved.schema!=='maimi-local-state-portability/v1'||saved.owner!==owner||!/^[a-f0-9]{64}$/.test(saved.profile_sha256))fail('LOCAL_STATE_MARKER_INVALID');}
 else if(!firstUse)fail('LOCAL_STATE_IMPORT_REQUIRED');
 return {
  async finishStartup(){
   if(!saved){const p=await bounded(profile);if(!p)fail('LOCAL_PROFILE_NOT_PREPARED');saved={schema:'maimi-local-state-portability/v1',owner,profile_sha256:digest(p)};await writeFile(marker,JSON.stringify(saved)+'\n',{flag:'wx',mode:0o600});}
  },
  async proof(identity,idle){
   if(!idle.draining||(await idle.busy()).busy!==false)fail('LOCAL_STATE_NOT_QUIESCENT');
   // .credentials.yaml is the existing generation-scoped credentials plugin's
   // local output. It is never copied; the replacement regenerates it.
   for(const [directory,allowed] of [[root,new Set(['dsh','profile.json','home','cache','.maimi-state-portability.json'])],[join(root,'dsh'),new Set(['profiles','storages','.credentials.yaml'])]]){
    const s=await kind(directory);if(!s||!s.isDirectory())fail('LOCAL_STATE_DIRECTORY_INVALID');
    for(const name of await readdir(directory)){if(!allowed.has(name))fail('LOCAL_STATE_UNMIGRATED_DATA');await kind(join(directory,name));}
   }
   // Home is not a disposable allowlist for arbitrary user output.
   const home=await kind(join(root,'home'));if(home&&(!home.isDirectory()||(await readdir(join(root,'home'))).length))fail('LOCAL_HOME_DATA_REQUIRES_EXPORT');
   if(await kind(join(root,'dsh/card-v1'))||await kind(join(root,'dsh/storages/workspace.json')))fail('LOCAL_STATE_UNMIGRATED_DATA');
   const p=await bounded(profile);if(!p||digest(p)!==saved.profile_sha256)fail('LOCAL_PREFERENCES_REQUIRE_EXPORT');
   return {protocol:'maimi-state-portability/v1',generation:identity.generation,pod_uid:identity.pod_uid,portable:true,local_preferences_unchanged:true,legacy_state_files_absent:true};
  }
 };
}
