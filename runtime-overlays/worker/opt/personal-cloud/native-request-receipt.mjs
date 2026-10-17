// Own one native prompt dispatch until its exact terminal receipt is known.
// Transport completion, browser disconnect, idle samples and elapsed time are
// not proof that the native controller has stopped or accepted a mutation.
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const MESSAGE='发送状态正在确认，请稍后查看对话后再发送。';
export function pendingNativeRequest(){
  return Object.assign(new Error(MESSAGE),{nativeCode:'session/cloud-send-pending',receiptPending:true});
}
function identity(value){
  if(!value||typeof value!=='object'||Object.keys(value).sort().join(',')!=='dispatchId,generation,sessionId'
    ||typeof value.dispatchId!=='string'||typeof value.sessionId!=='string'
    ||!UUID.test(value.dispatchId)||!SID.test(value.sessionId)||!Number.isSafeInteger(value.generation)||value.generation<1)
    throw Error('native prompt receipt identity invalid');
  return Object.freeze({dispatchId:value.dispatchId,sessionId:value.sessionId,generation:value.generation});
}
function terminal(value,key){
  if(!value||typeof value!=='object'||value.dispatch_id!==key.dispatchId||value.session_id!==key.sessionId
    ||value.generation!==key.generation||value.state!=='done'||typeof value.result?.ok!=='boolean')return false;
  // Prompt receipts are small control envelopes, never file payloads or a
  // model stream. Reject unexpected bodies instead of retaining them forever.
  try{return Buffer.byteLength(JSON.stringify(value))<=65536;}catch{return false;}
}

export class NativeRequestReceipts {
  constructor(){this.pending=new Map();this.seen=new Map();this.generations=new Map();this.recovery=null;}
  get busy(){return this.pending.size>0;}
  assertAdmission(sessionId){if([...this.pending.values()].some(x=>!sessionId||x.key.sessionId===sessionId))throw pendingNativeRequest();}

  // Deliberately synchronous until registration: the caller can transfer its
  // dispatch-counter ownership only after this method returns its Promise.
  // perform must replay this exact identity/payload; it must not allocate a new
  // native request ID. onSettled is synchronous and called at most once.
  run(value,perform,onSettled){
    const key=identity(value);
    if(typeof perform!=='function'||typeof onSettled!=='function')throw Error('native receipt callbacks invalid');
    this.assertAdmission(key.sessionId);
    if(key.generation<(this.generations.get(key.sessionId)||0))throw Error('native receipt generation retired');
    for(const [id,old]of this.seen)if(old.sessionId===key.sessionId&&old.generation<key.generation)this.seen.delete(id);
    this.generations.set(key.sessionId,key.generation);
    if(this.seen.has(key.dispatchId)||this.seen.size>=1024)throw Error('native receipt identity reused or full');
    this.seen.set(key.dispatchId,key);
    const entry={key,perform,onSettled,inFlight:null,settled:false,releaseFailed:false};
    this.pending.set(key.dispatchId,entry);
    return this.attempt(entry);
  }

  attempt(entry){
    if(entry.inFlight)return entry.inFlight;
    if(entry.releaseFailed)return Promise.reject(pendingNativeRequest());
    const attempt=Promise.resolve().then(async()=>{
      let value;
      try{value=await entry.perform(entry.key);}
      catch{throw pendingNativeRequest();}
      if(!terminal(value,entry.key))throw pendingNativeRequest();
      if(!entry.settled){
        entry.settled=true;
        try{
          const result=entry.onSettled(value);
          if(result&&typeof result.then==='function')throw Error('receipt settlement must be synchronous');
        }catch{
          // A failed local release callback is never repeated or silently
          // cleared. Keeping the fence is safer than authorizing a successor.
          entry.releaseFailed=true;throw pendingNativeRequest();
        }
      }
      this.pending.delete(entry.key.dispatchId);
      entry.perform=null;entry.onSettled=null;
      return value;
    });
    entry.inFlight=attempt;
    // Use both handlers rather than an ignored .finally() Promise, which would
    // otherwise generate an unhandled rejection when a transport attempt fails.
    attempt.then(()=>{entry.inFlight=null;},()=>{entry.inFlight=null;});
    return attempt;
  }

  async recover(){
    if(this.recovery)return this.recovery;
    this.recovery=(async()=>{
      for(const entry of this.pending.values()){
        try{await this.attempt(entry);}catch{/* Retain this exact receipt fence. */}
      }
    })();
    try{await this.recovery;}finally{this.recovery=null;}
  }
}
