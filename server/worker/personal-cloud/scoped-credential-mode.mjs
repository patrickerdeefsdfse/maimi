import {constants} from 'node:fs';
import {lstat,open} from 'node:fs/promises';

// Kubelet fsGroup may widen an existing native credential file to 0660 on
// remount. Before any native process starts, narrow only this owned file.
// Neither the contents nor any other file in DSH_HOME are inspected.
export async function normalizeScopedCredentialMode(home='/state/dsh',ownerUid=1000){
 const refuse=()=>{throw new Error('SCOPED_CREDENTIAL_METADATA_UNSAFE');};
 if(process.getuid()!==ownerUid||home!=='/state/dsh')refuse();
 const root=await lstat('/state'),parent=await lstat(home);
 if(!root.isDirectory()||root.isSymbolicLink()||!parent.isDirectory()||parent.isSymbolicLink()
   ||parent.uid!==ownerUid||(parent.mode&0o002))refuse();
 const path=home+'/.credentials.yaml';
 let first;
 try{first=await lstat(path);}catch(error){if(error.code==='ENOENT')return {status:'ABSENT'};throw error;}
 const valid=info=>info.isFile()&&!info.isSymbolicLink()&&info.uid===ownerUid&&info.nlink===1;
 const same=(a,b)=>a.dev===b.dev&&a.ino===b.ino;
 if(!valid(first))refuse();
 const fd=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
 try{
  const current=await fd.stat();
  if(!valid(current)||!same(first,current))refuse();
  const rebound=await lstat(path),parentNow=await lstat(home),rootNow=await lstat('/state');
  if(!valid(rebound)||!same(current,rebound)||!same(parent,parentNow)||!same(root,rootNow))refuse();
  await fd.chmod(0o600);
  const after=await fd.stat(),boundAfter=await lstat(path);
  if(!valid(after)||!same(current,after)||!same(after,boundAfter)||(after.mode&0o7777)!==0o600)refuse();
  return {status:'PRIVATE_MODE_VERIFIED'};
 }finally{await fd.close();}
}
