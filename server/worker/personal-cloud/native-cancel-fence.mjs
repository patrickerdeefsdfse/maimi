// A native cancellation is a mutation even when no model generation is active.
// Keep admission and idle fenced until its exact HTTP response is fully read.
// Recovery retries an explicitly idempotent operation with its original receipt
// identity. An idle snapshot or elapsed timeout alone never clears the fence.
export class NativeCancelFence {
  constructor({enter=()=>()=>{}}={}){this.enter=enter;this.pending=new Set();this.failed=false;}
  get busy(){return this.pending.size>0;}
  assertAdmission(sessionId){if([...this.pending].some(x=>!sessionId||x.sessionId===sessionId))throw Error('云端任务正在结束，请稍后再试。');}
  async waitForOwner(sessionId,lease){
    const entry=[...this.pending].find(item=>item.sessionId===sessionId && item.lease===lease && lease);
    if(!entry)return false;
    if(!await entry.finished.promise)throw Object.assign(Error('上一轮仍在停止，请稍后重试。'),{nativeCode:'session/cancel-pending'});
    return true;
  }
  async run(operation,{recoverable=false,sessionId,lease}={}){
    if([...this.pending].some(x=>!sessionId||x.sessionId===sessionId)||this.pending.size>=4)throw Error('云端任务正在结束，请稍后再试。');
    const entry={sessionId,lease,finish:this.enter({work:true}),finished:Promise.withResolvers(),operation,recoverable,failed:false};this.pending.add(entry);
    try{const value=await operation();this.pending.delete(entry);entry.finish();entry.finished.resolve(true);return value;}
    catch(error){this.failed=true;entry.failed=true;entry.finished.resolve(false);throw error;}
  }
  async recover(){
    if(this.recovery)return this.recovery;
    this.recovery=(async()=>{
      for(const entry of this.pending){
        if(!entry.failed||!entry.recoverable)continue;
        entry.finished=Promise.withResolvers();
        try{await entry.operation();this.pending.delete(entry);entry.finish();entry.finished.resolve(true);}
        catch{entry.finished.resolve(false);/* Keep the exact operation fenced. */}
      }
      this.failed=[...this.pending].some(entry=>entry.failed);
    })();
    try{await this.recovery;}finally{this.recovery=null;}
  }
}
