'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {pathToFileURL}=require('node:url');

// Native Node cannot import app.asar. Materialize this one shipped module only
// inside the account directory whose exclusive lease is already held.
function modelPolicyPatch(home, modelBase, verifiedSource) {
  const endpoint=new URL(modelBase);
  if(endpoint.protocol!=='http:'||endpoint.hostname!=='127.0.0.1'||!endpoint.port||endpoint.pathname!=='/__megmeet/model/v1'||endpoint.search||endpoint.hash||endpoint.username||endpoint.password)throw Error('MODEL_POLICY_ENDPOINT_INVALID');
  const dir=path.join(home,'megmeet-managed-plugins');fs.mkdirSync(dir,{recursive:true});
  if(!fs.lstatSync(dir).isDirectory()||fs.lstatSync(dir).isSymbolicLink())throw Error('MODEL_POLICY_PATH_INVALID');
  const target=path.join(dir,'enterprise-model-policy.mjs');
  if(fs.existsSync(target)&&(!fs.lstatSync(target).isFile()||fs.lstatSync(target).isSymbolicLink()))throw Error('MODEL_POLICY_PATH_INVALID');
  const temp=target+'.'+crypto.randomUUID()+'.tmp';
  try{fs.writeFileSync(temp,fs.readFileSync(verifiedSource),{flag:'wx'});fs.renameSync(temp,target);}
  finally{if(fs.existsSync(temp))fs.unlinkSync(temp);}
  return [{insert:[{id:'megmeet-model-policy',name:pathToFileURL(target).href,config:{modelBase,apiKeyEnv:'MEGMEET_MODEL_TOKEN'}}]}];
}
module.exports={modelPolicyPatch};
