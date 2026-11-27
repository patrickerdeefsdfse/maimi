// Per-workspace file transfer admission, independent of login/account limits.
// Acquire before reading a request body; queued requests hold no decoded files.
export class FileTransferGate {
  #busy=false; #queue=[];
  constructor({maximumQueued=8,waitMs=10000}={}){this.maximumQueued=maximumQueued;this.waitMs=waitMs;}
  async acquire(signal){
    signal?.throwIfAborted();
    if(!this.#busy){this.#busy=true;return this.#release();}
    if(this.#queue.length>=this.maximumQueued)throw Object.assign(Error('project_file_transfer_busy'),{code:'project_file_transfer_busy',status:429});
    return new Promise((resolve,reject)=>{
      const row={resolve,reject,signal};
      const remove=error=>{const at=this.#queue.indexOf(row);if(at<0)return;this.#queue.splice(at,1);clearTimeout(row.timer);signal?.removeEventListener('abort',row.abort);reject(error);};
      row.abort=()=>remove(signal.reason||new DOMException('Cancelled','AbortError'));
      row.timer=setTimeout(()=>remove(Object.assign(Error('project_file_transfer_busy'),{code:'project_file_transfer_busy',status:429})),this.waitMs);
      signal?.addEventListener('abort',row.abort,{once:true});this.#queue.push(row);
      if(signal?.aborted)row.abort();
    });
  }
  #release(){let done=false;return()=>{if(done)return;done=true;const next=this.#queue.shift();if(next){clearTimeout(next.timer);next.signal?.removeEventListener('abort',next.abort);next.resolve(this.#release());}else this.#busy=false;};}
}
export function releaseWithResponse(response,release){
  if(!response.body){release();return response;}
  const reader=response.body.getReader();
  const body=new ReadableStream({async pull(controller){try{const {done,value}=await reader.read();if(done){release();controller.close();}else controller.enqueue(value);}catch(error){release();controller.error(error);}},
    async cancel(reason){try{await reader.cancel(reason);}finally{release();}}});
  return new Response(body,{status:response.status,statusText:response.statusText,headers:response.headers});
}
export async function withNodeTransfer(gate,request,response,operation){
  const controller=new AbortController();let release,workDone=false,deliveryDone=false;
  const finish=()=>{if(workDone&&deliveryDone){release?.();release=null;request.off('aborted',abort);response.off('close',closed);response.off('finish',delivered);}};
  const abort=()=>{controller.abort();deliveryDone=true;finish();};
  const closed=()=>{controller.abort();deliveryDone=true;finish();};
  const delivered=()=>{deliveryDone=true;finish();};
  request.once('aborted',abort);response.once('close',closed);response.once('finish',delivered);
  try{
    if(request.aborted||response.destroyed)controller.abort();
    release=await gate.acquire(controller.signal);controller.signal.throwIfAborted();
    return await operation(controller.signal);
  }finally{
    workDone=true;if(response.writableFinished||response.destroyed||request.aborted)deliveryDone=true;
    if(!release){request.off('aborted',abort);response.off('close',closed);response.off('finish',delivered);}
    finish();
  }
}
