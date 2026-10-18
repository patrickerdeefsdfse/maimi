import {readdir,access,writeFile} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
const here=path.dirname(fileURLToPath(import.meta.url));
const root=path.resolve(here,'../../upstream/runtime-win/node_modules/@deepseek-ai');
const {validateNativeRpc}=await import(pathToFileURL(path.resolve(here,'../../project/runtime-overlays/worker/opt/personal-cloud/task-authority.mjs')).href);
const results=[];
for(const name of await readdir(root)){
 const filename=path.join(root,name,'lib/typert.host.js');
 try{await access(filename);}catch{continue;}
 const {TYPERT}=await import(pathToFileURL(filename).href);
 for(const descriptor of TYPERT?.invocations||[]){
  const endpoint=descriptor.namespace+'/'+descriptor.method;
  if(!/browser|approval|permission|cordis|agent|schedule|goal|workflow|plugin|remoteAccess|interaction/i.test(endpoint))continue;
  let outcome;
  try{outcome=validateNativeRpc('/api/'+endpoint,'POST',{type:'client-request',rpcId:'test',method:endpoint,payload:{args:{}}},'test').readOnly?'READ_ONLY':'MUTATION_LEASE_GATED';}
  catch{outcome='BLOCKED_BY_COMPANY_POLICY';}
  results.push({package:name,endpoint,mode:descriptor.mode||'unary',policy:outcome});
 }
}
await writeFile(path.join(here,'native-rpc-inventory.json'),JSON.stringify({schema:'r65-native-rpc-policy-audit-v1',runtime:'0.2.1-rc.2',items:results},null,2)+'\n');
console.log(JSON.stringify(results,null,2));
