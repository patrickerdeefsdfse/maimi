import {AsyncLocalStorage} from 'node:async_hooks';

const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const deny=code=>{throw Object.assign(Error(code),{code});};

// One instance belongs to the native DSH process. claim/runPrompt/revoke are
// private control-plane calls, never browser RPC or model tool arguments.
export function createNativeModelScopes({agents,issueCredential,releaseCredential}) {
  const generations=new Map();
  const initiator=new AsyncLocalStorage();
  const streamScope=new AsyncLocalStorage();
  function current(entry){
    if(!entry||generations.get(entry.root)!==entry||entry.revoked)
      deny('CLOUD_MODEL_GENERATION_REVOKED');
    return entry;
  }
  function rootOf(session){
    if(!SID.test(session||''))deny('CLOUD_MODEL_SESSION_REQUIRED');
    let agent=agents.get(session);
    if(!agent)deny('CLOUD_MODEL_AGENT_NOT_LIVE');
    const all=agents.list(),roots=new Set(agents.roots());
    if(all.length>256)deny('CLOUD_MODEL_AGENT_LIMIT');
    const seen=new Set();
    while(!roots.has(agent)){
      if(seen.has(agent))deny('CLOUD_MODEL_OWNERSHIP_INVALID');
      seen.add(agent);
      const parents=all.filter(parent=>agents.isOwnedBy(agent.id,parent));
      if(parents.length!==1)deny('CLOUD_MODEL_OWNERSHIP_INVALID');
      agent=parents[0];
    }
    return String(agent.id);
  }
  function claim(root,generation){
    if(!SID.test(root||'')||!Number.isSafeInteger(generation)||generation<1)
      deny('CLOUD_MODEL_CLAIM_INVALID');
    const old=generations.get(root);
    if(old){
      if(old.generation===generation&&!old.revoked)return old;
      if(generation<=old.generation||!old.revoked||old.streams)
        deny('CLOUD_MODEL_GENERATION_BUSY');
    }
    const entry={root,generation,revoked:false,streams:0};
    generations.set(root,entry);return entry;
  }
  function runPrompt(root,generation,request,invoke){
    const entry=current(generations.get(root));
    if(entry.generation!==generation||request?.sessionId!==root||typeof invoke!=='function')
      deny('CLOUD_MODEL_PROMPT_SCOPE_INVALID');
    // The Promise and every native driver spawned by invoke inherit the exact
    // generation. A late title/child never falls back to the latest generation.
    return initiator.run(entry,invoke);
  }
  function intercept(options,next){
    const entry=current(initiator.getStore());
    if(rootOf(options?.sessionId)!==entry.root)
      deny('CLOUD_MODEL_CROSS_SESSION');
    if(options.purpose!==undefined&&!['compaction','session-title'].includes(options.purpose))
      deny('CLOUD_MODEL_PURPOSE_INVALID');
    options.signal?.throwIfAborted();
    const call={entry,sessionId:String(options.sessionId),purpose:options.purpose??'conversation',
      tokens:new Set(),closed:false};
    let used=false;
    return {[Symbol.asyncIterator](){
      if(used)deny('CLOUD_MODEL_STREAM_REUSED');used=true;
      current(entry);entry.streams++;
      let iterator,done=false;
      const scoped=operation=>initiator.run(entry,()=>streamScope.run(call,operation));
      const finish=async()=>{
        if(done)return;done=true;call.closed=true;entry.streams--;
        await Promise.allSettled([...call.tokens].map(token=>releaseCredential(token)));
      };
      const advance=async(method,value)=>{
        try{
          if(method==='next'){current(entry);options.signal?.throwIfAborted();}
          if(!iterator)iterator=scoped(()=>next()[Symbol.asyncIterator]());
          const result=iterator[method]?await scoped(()=>iterator[method](value)):{done:true,value};
          if(result.done)await finish();
          return result;
        }catch(error){
          if(iterator?.return)try{await scoped(()=>iterator.return());}catch{}
          await finish();throw error;
        }
      };
      return {next:value=>advance('next',value),return:value=>advance('return',value),
        throw:value=>advance('throw',value),[Symbol.asyncIterator](){return this;}};
    }};
  }
  async function resolveCredential(){
    const call=streamScope.getStore();
    if(!call||call.closed)deny('CLOUD_MODEL_STREAM_SCOPE_REQUIRED');
    current(call.entry);
    const token=await issueCredential({rootSessionId:call.entry.root,sessionId:call.sessionId,
      generation:call.entry.generation,purpose:call.purpose});
    if(typeof token!=='string'||!/^mmt_[A-Za-z0-9_-]{43}$/.test(token))deny('CLOUD_MODEL_TICKET_INVALID');
    call.tokens.add(token);current(call.entry);
    if(call.closed){await releaseCredential(token);deny('CLOUD_MODEL_STREAM_CLOSED');}
    return {value:token,source:'megmeet-native-task'};
  }
  function revoke(root,generation){
    const entry=generations.get(root);
    if(!entry||entry.generation!==generation)deny('CLOUD_MODEL_CANCEL_GENERATION_INVALID');
    entry.revoked=true;
  }
  function captureToolScope(agent){
    const entry=current(initiator.getStore());
    if(!agent||agents.get(agent.id)!==agent||rootOf(agent.id)!==entry.root)
      deny('CLOUD_TOOL_CROSS_SESSION');
    return Object.freeze({rootSessionId:entry.root,sessionId:String(agent.id),generation:entry.generation});
  }
  function validateToolScope(scope){
    const entry=current(initiator.getStore());
    if(scope?.rootSessionId!==entry.root||scope.generation!==entry.generation||rootOf(scope.sessionId)!==entry.root)
      deny('CLOUD_TOOL_CROSS_SESSION');
    return scope;
  }
  return {claim,runPrompt,intercept,resolveCredential,revoke,captureToolScope,validateToolScope,
    busy:()=>[...generations.values()].some(value=>value.streams>0),
    metadata:()=>[...generations.values()].map(({root,generation,revoked,streams})=>({root,generation,revoked,streams}))};
}

// Preserve every official credential capability except the private local
// model reference. No account/API credential or ephemeral ticket is persisted.
export function scopedCredentialProvider(LocalCredentialProvider,scopes){
  return class ScopedCredentialProvider extends LocalCredentialProvider {
    resolve(ref){return ref==='MEGMEET_MODEL_TOKEN'?scopes.resolveCredential():super.resolve(ref);}
    describe(ref){return ref==='MEGMEET_MODEL_TOKEN'?Promise.resolve({configured:true,source:'megmeet-native-task',writable:false}):super.describe(ref);}
    set(ref,value){if(ref==='MEGMEET_MODEL_TOKEN')deny('CLOUD_MODEL_CREDENTIAL_READ_ONLY');return super.set(ref,value);}
    unset(ref){if(ref==='MEGMEET_MODEL_TOKEN')deny('CLOUD_MODEL_CREDENTIAL_READ_ONLY');return super.unset(ref);}
  };
}
