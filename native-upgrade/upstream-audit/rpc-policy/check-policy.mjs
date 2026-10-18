import assert from 'node:assert/strict';
import {readdir,readFile,writeFile,access} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
import {TaskAuthority,READ_RPC,validateNativeRpc} from './task-authority.mjs';
import {READ_RPC as oldReads,validateNativeRpc as oldValidate} from './task-authority.before.mjs';
const here=path.dirname(fileURLToPath(import.meta.url));
const runtime=path.resolve(process.env.DSH_R65_RUNTIME||path.resolve(here,'../../upstream/runtime-win')),modules=path.join(runtime,'node_modules/@deepseek-ai');
const require=createRequire(path.join(runtime,'package.json'));
const pkg=name=>import(pathToFileURL(require.resolve(name)).href);
const descriptors=[],packages=new Map(),pins={};
for(const name of await readdir(modules)){
 const file=path.join(modules,name,'lib/typert.host.js');try{await access(file);}catch{continue;}
 const {TYPERT}=await import(pathToFileURL(file).href);if(!TYPERT)continue;
 packages.set(name,TYPERT);
 pins[name]=createHash('sha256').update(await readFile(file)).digest('hex');
 for(const d of TYPERT.invocations||[])descriptors.push({...d,package:name,endpoint:d.namespace+'/'+d.method});
}
const byEndpoint=new Map(descriptors.map(d=>[d.endpoint,d]));
const wrap=(endpoint,args={})=>({type:'client-request',rpcId:'r65-synthetic',method:endpoint,payload:{args}});
const check=(endpoint,args={})=>validateNativeRpc('/api/'+endpoint,'POST',wrap(endpoint,args),'workspace-synthetic');
const mustRead=['agentPresets/list','agentPresets/read','permissionPresets/catalog','pluginInventory/list',
 'schedule/catalog','schedule/list','schedule/history','goals/get','pluginManager/inspect','pluginManager/listPlugins',
 'pluginManager/listBundles','pluginManager/listVersionExemptions','pluginManager/registries','pluginManager/waitForInstall',
 'dynamicCordisRunner/getClientCode','dynamicCordisRunner/inventory','pluginMarket/listIndexes','pluginMarket/search',
 'pluginRegistryProbe/fastest','remoteAccess/follow'];
const mustDeny=['goals/create','goals/edit','goals/resume','goals/pause','goals/clear','goals/complete',
 'schedule/update','schedule/delete','dynamicCordisRunner/runHostHalf','dynamicCordisRunner/invoke',
 'dynamicCordisRunner/settleUserRun','dynamicCordisRunner/resolveRequestRun',
 'dynamicCordisRunner/reportClientGuardFailure','dynamicCordisRunner/reportRenderFailure',
 'dynamicCordisRunner/resolveInspectQuery','dynamicCordisRunner/syncInspectManifest',
 'dynamicCordisRunner/undefineFromPanel','dynamicCordisRunner/stopFromPanel',
 'pluginManager/installBundle','pluginManager/setPluginEnabled','pluginManager/setBundleEnabled',
 'pluginManager/removeBundle','pluginManager/setVersionExemption','pluginManager/cancelInstall','pluginMarket/setIndexes'];
for(const e of mustRead){assert.ok(byEndpoint.has(e),'actual descriptor missing '+e);assert.equal(check(e).readOnly,true,e);}
for(const e of mustDeny){assert.ok(byEndpoint.has(e),'actual descriptor missing '+e);assert.throws(()=>check(e),/公司统一管理/,e);}
for(const e of oldReads){assert.ok(READ_RPC.has(e),'lost old read alias '+e);assert.equal(check(e).readOnly,true,e);}
for(const e of ['goals/unknownFutureMethod','schedule/newRecurringAction','pluginManager/newActivate','dynamicCordisRunner/newEvaluate','pluginMarket/newSources'])assert.throws(()=>check(e));
assert.equal(check('$events/result').readOnly,false);
for(const e of ['$events/run','$events/result/extra','$events/result%2f','%24events/result'])assert.throws(()=>check(e));
for(const e of ['goals%2fcreate','%67oals/create','goals/create/extra','goals//create','goals/../create'])assert.throws(()=>check(e));
assert.throws(()=>validateNativeRpc('/api/goals/get','POST',wrap('goals/create'),'workspace-synthetic'));
// The worker skips mutation ownership checks for reads. Two independently
// leased running roots must therefore not lock each other's catalogs/history.
const authority=new TaskAuthority(),A='11111111-1111-4111-8111-111111111111',B='22222222-2222-4222-8222-222222222222';
const leaseA='mcl_'+'A'.repeat(43),leaseB='mcl_'+'B'.repeat(43),leaseC='mcl_'+'C'.repeat(43);
authority.claim(leaseA,A,{protocol:2,busy:false,activeRoots:[]});
authority.claim(leaseB,B,{protocol:2,busy:true,activeRoots:[A]});
const revision=authority.revision;
let readCases=0;
for(const lease of [leaseA,leaseB,leaseC])for(const e of mustRead){const command=check(e);if(!command.readOnly)authority.assertMutation(lease,command.request?.sessionId);readCases++;}
assert.equal(authority.revision,revision);assert.equal(authority.list().length,2);
assert.throws(()=>authority.assertMutation(leaseB,A));assert.throws(()=>authority.assertMutation(leaseC));
authority.assertMutation(leaseA,A);authority.assertMutation(leaseB,B);
// Real latest Gateway + generated (unmodified) descriptors: a raw stream
// carrier cannot invoke the unary managed operations, even for an owner Peer.
const [{Context,Service},{Peer},{default:Gateway},{default:Registry},{default:RemoteAccess},{bindTypertRemote},{default:Agents},{Session}]=await Promise.all([
 '@deepseek-ai/cordis','@deepseek-ai/dsh-client-connection','@deepseek-ai/dsh-api-gateway','@deepseek-ai/dsh-typert-registry',
 '@deepseek-ai/dsh-api-remote-access','@deepseek-ai/dsh-typert-protocol','@deepseek-ai/dsh-agent','@deepseek-ai/dsh-session'].map(pkg));
const ctx=new Context(),fibers=[],called=[];let peer,detach;
const use=async(plugin)=>{const f=ctx.plugin(plugin);fibers.push(f);await f;};
const targets=[
 {pkg:'dsh-goal',namespace:'goals',method:'create',args:{agentId:A,request:{objective:'SYNTHETIC NEVER EXECUTED'}}},
 {pkg:'dsh-cordis-host-runner',namespace:'dynamicCordisRunner',method:'runHostHalf',args:{agentId:A,pluginId:'fixture',packageId:'fixture',mode:'run',requestId:null,approveFutureVersions:false}},
 {pkg:'dsh-plugin-manager',namespace:'pluginManager',method:'installBundle',args:{spec:'SYNTHETIC NEVER INSTALLED'}},
];
try{
 await use(Registry);await use(RemoteAccess);await use(Gateway);await use(Agents);
 const agent={id:A,ctx,session:Session.create(A),status:'idle'};detach=ctx.agents.enter(agent,undefined);
 for(const target of targets){
  const info=packages.get(target.pkg);ctx.typert.register(info);
  const d=info.invocations.find(x=>x.namespace===target.namespace&&x.method===target.method);assert.equal(d.mode,undefined);
  class Boundary extends Service{
   constructor(context){super(context,target.namespace);this.typertRemote=bindTypertRemote(this,target.namespace,{namespace:target.namespace});}
  }
  Boundary.prototype[d.implementation||d.method]=function(){called.push(target.namespace+'/'+target.method);throw Error('MUTATION_MUST_NOT_EXECUTE');};
  await use(Boundary);
 }
 peer=new Peer(ctx,{principal:{kind:'owner'},interactions:'none'});
 for(const t of targets){await assert.rejects(ctx.typertGateway.stream({peer,namespace:t.namespace,method:t.method,args:t.args}),e=>e.code==='gateway/signature-invalid'&&e.message.includes('unary Remote methods'));}
 assert.deepEqual(called,[]);
}finally{if(peer)await peer.dispose();detach?.();for(const f of fibers.reverse())await f.dispose();}
const selected=descriptors.filter(d=>/browser|approval|permission|cordis|agent|schedule|goal|workflow|plugin|remoteAccess|interaction/i.test(d.endpoint));
function classification(validator,e){try{return validator('/api/'+e,'POST',wrap(e),'workspace-synthetic').readOnly?'READ_ONLY':'MUTATION_LEASE_GATED';}catch{return 'BLOCKED_BY_COMPANY_POLICY';}}
const result={schema:'r65-native-rpc-policy-candidate-v1',status:'PASSED',runtime:'0.2.1-rc.2',company_requests:0,
 descriptor_count:descriptors.length,reviewed_descriptor_count:selected.length,required_native_reads:mustRead.length,required_native_denials:mustDeny.length,
 legacy_reads_preserved:oldReads.size,two_lease_read_cases:readCases,two_lease_authority_unchanged:true,foreign_mutation_still_denied:true,
 actual_native_stream_rejections:targets.map(t=>t.namespace+'/'+t.method),mutating_service_calls:called.length,
 unknown_managed_actions_rejected:true,malformed_endpoint_rejected:true,
 native_approval_result_endpoint_allowed_as_mutation:true,reserved_namespace_others_rejected:true,
 candidate_sha256:createHash('sha256').update(await readFile(path.join(here,'task-authority.mjs'))).digest('hex'),
 actual_descriptor_hashes:pins,
 items:selected.map(d=>({package:d.package,endpoint:d.endpoint,mode:d.mode||'unary',before:classification(oldValidate,d.endpoint),after:classification(validateNativeRpc,d.endpoint)}))};
await writeFile(path.join(here,'native-rpc-policy-result.json'),JSON.stringify(result,null,2)+'\n');
console.log(JSON.stringify({...result,actual_descriptor_hashes:undefined,items:undefined}));
