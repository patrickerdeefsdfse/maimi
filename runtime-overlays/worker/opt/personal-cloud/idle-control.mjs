// A fenced idle snapshot. Network activity alone does not keep a worker alive.
// Admission is synchronous so an upload or prompt cannot cross a draining fence
// while the asynchronous native task snapshot is being read.
export class IdleController {
  constructor({sample,localBusy=()=>false,onFence=()=>{},onResume=()=>{}}) {
    this.sample=sample;this.localBusy=localBusy;this.onFence=onFence;this.onResume=onResume;
    this.inFlight=0;this.workInFlight=0;this.draining=false;this.epoch=0;this.pending=null;
  }
  enter({work=false}={}) {
    if(this.draining)throw Object.assign(Error('云端工作区正在释放，请点击重新进入。'),{code:'cloud-idle-release'});
    this.inFlight++;if(work)this.workInFlight++;
    let finished=false;
    const finish=()=>{if(!finished){finished=true;this.inFlight--;if(work)this.workInFlight--;}};
    finish.work=()=>{if(!finished&&!work){work=true;this.workInFlight++;}};
    return finish;
  }
  view(busy) { return {protocol:1,busy,draining:this.draining}; }
  async busy() {
    const state=await this.sample();
    if(!state||state.protocol!==2||typeof state.busy!=='boolean'||!Array.isArray(state.activeRoots))
      throw Error('cloud task state unavailable');
    return this.view(state.busy||this.workInFlight>0||Boolean(this.localBusy()));
  }
  resume() {
    this.epoch++;this.draining=false;this.pending=null;this.onResume();
    return this.view(this.workInFlight>0||Boolean(this.localBusy()));
  }
  async drain() {
    if(this.pending)return this.pending;
    if(this.draining)return this.view(false);
    if(this.inFlight>0||this.localBusy())return this.view(this.workInFlight>0||Boolean(this.localBusy()));
    const epoch=++this.epoch;this.draining=true;
    try{this.onFence();}catch{this.resume();return this.view(true);}
    this.pending=(async()=>{
      try{
        const value=await this.busy();
        if(epoch!==this.epoch)return this.view(true);
        if(value.busy){this.resume();return this.view(true);}
        return this.view(false);
      }catch{
        if(epoch===this.epoch)this.resume();
        return this.view(true);
      }finally{if(epoch===this.epoch)this.pending=null;}
    })();
    return this.pending;
  }
}

// A disconnect does not imply that an asynchronous handler has stopped.
// Keep admission held until both its preparation/control work and transport
// have finished, so a late continuation cannot mutate a fenced worker.
export function holdIdleRequest(controller,req,res,{work=false}={}) {
  const finish=controller.enter({work});let processing=true,ended=false;
  const end=()=>{ended=true;if(!processing)finish();};
  res.once('finish',end);res.once('close',end);req.once('aborted',end);
  return {work:finish.work,handlerDone(){processing=false;if(ended)finish();}};
}
