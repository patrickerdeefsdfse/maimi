// A native cancellation is a mutation even when no model generation is active.
// Keep admission and idle fenced until its exact HTTP response is fully read.
// Recovery retries an explicitly idempotent operation with its original receipt
// identity. An idle snapshot or elapsed timeout alone never clears the fence.
export class NativeCancelFence {
  constructor({enter=()=>()=>{}}={}){this.enter=enter;this.pending=new Set();this.failed=false;}
  get busy(){return this.pending.size>0;}
  assertAdmission(sessionId){if([...this.pending].some(x=>!sessionId||x.sessionId===sessionId))throw Error('云端任务正在结束，请稍后再试。');}
  async run(operation,{recoverable=false,sessionId}={}){
    if([...this.pending].some(x=>!sessionId||x.sessionId===sessionId)||this.pending.size>=4)throw Error('云端任务正在结束，请稍后再试。');
    const entry={sessionId,finish:this.enter({work:true}),operation,recoverable,failed:false};this.pending.add(entry);
    try{const value=await operation();this.pending.delete(entry);entry.finish();return value;}
    catch(error){this.failed=true;entry.failed=true;throw error;}
  }
  async recover(){
    if(this.recovery)return this.recovery;
    this.recovery=(async()=>{
      for(const entry of this.pending){
        if(!entry.failed||!entry.recoverable)continue;
        try{await entry.operation();this.pending.delete(entry);entry.finish();}catch{/* Keep the exact operation fenced. */}
      }
      this.failed=[...this.pending].some(entry=>entry.failed);
    })();
    try{await this.recovery;}finally{this.recovery=null;}
  }
}
