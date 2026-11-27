import {randomUUID} from 'node:crypto';
import {Readable,Writable} from 'node:stream';
import {setTimeout as delay} from 'node:timers/promises';

export class RemoteProcess {
 constructor({spec,call,forget=true}){
  // A cancelled request must never enqueue or launch a process.
  spec.signal?.throwIfAborted();
  this.spec=spec;this.call=call;this.id=randomUUID();this.generation=undefined;this.stopped=false;this.empty=false;
  this.snap={stdout:{text:'',nextOffset:0,lossy:false},stderr:{text:'',nextOffset:0,lossy:false}};
  this.positions={stdout:0,stderr:0};this.collected={};
  this.remoteOffsets={stdout:0,stderr:0};
  for(const key of ['stdout','stderr']){
   if(spec.stdio[key]==='pipe'){this[key]=new Readable({read(){}});this[key].on('error',()=>{});}
   else if(typeof spec.stdio[key]==='object')this.collected[key]={readFrom:offset=>this.read(key,offset)};
  }
  this.control=undefined;
  if(spec.stdio.stdin==='pipe'){this.stdin=new Writable({write:(chunk,_enc,cb)=>{this.sendInput(chunk).then(()=>cb(),cb);},final:cb=>{this.sendInput(Buffer.alloc(0),true).then(()=>cb(),cb);}});this.stdin.on('error',()=>{});}
  this.done=this.run(forget);this.done.catch(()=>{});
  if(spec.signal){if(spec.signal.aborted)this.terminate();else spec.signal.addEventListener('abort',()=>this.terminate(),{once:true});}
 }
 read(key,offset){
  const s=this.snap[key],buf=Buffer.from(s.text),start=s.nextOffset-buf.length;
  return {...s,text:buf.subarray(Math.max(0,offset-start)).toString('utf8'),lossy:offset<start};
 }
 async request(operation,args){return this.call(operation,{job_id:this.id,...args},{generation:this.generation});}
 async sendInput(data,end=false){await this.started;for(let pos=0;pos<data.length;pos+=65536)await this.request('process.stdin',{data:data.subarray(pos,pos+65536).toString('base64'),end:end&&pos+65536>=data.length});if(!data.length&&end)return this.request('process.stdin',{data:'',end:true});}
 async run(forget){
  this.spec.signal?.throwIfAborted();
  const clean={...this.spec};delete clean.signal;
  this.started=this.call('process.spawn',{job_id:this.id,spec:clean},{signal:this.spec.signal}).then(r=>{this.generation=r.generation;return r;});
  try{
   await this.started;
   if(this.stopped)await this.request('process.terminate',{});
   for(;;){
    const response=await this.request('process.poll',{offsets:this.remoteOffsets}),s=response.value;
    for(const key of ['stdout','stderr'])if(s[key]){
     this.remoteOffsets[key]=s[key].nextOffset;
     if(this[key]){if(s[key].lossy)throw Error('EXECUTION_STREAM_OVERFLOW');const bytes=Buffer.from(s[key].base64||'','base64');if(this[key].readableLength+bytes.length>2*1024*1024)throw Error('EXECUTION_STREAM_CONSUMER_STALLED');if(bytes.length)this[key].push(bytes);}
     else {const old=s[key].lossy?'':this.snap[key].text,cap=this.spec.stdio[key]?.maxBytes||65536;let bytes=Buffer.from(old+s[key].text);if(bytes.length>cap){let start=bytes.length-cap;while(start<bytes.length&&(bytes[start]&0xc0)===0x80)start++;bytes=bytes.subarray(start);}this.snap[key]={...s[key],text:bytes.toString('utf8')};}
    }
    this.empty=s.empty;
    if(s.finished){
     if(s.error)throw Error('EXECUTION_PROCESS_FAILED');
     if(s.empty){for(const key of ['stdout','stderr'])this[key]?.push(null);if(forget)await this.request('process.forget',{});return s.outcome;}
    }
    await delay(s.finished?100:750);
   }
  }catch(error){
   this.stopped=true;
   if(this.generation)await this.request('process.terminate',{}).catch(()=>{});
   for(const key of ['stdout','stderr'])this[key]?.destroy(error);
   throw error;
  }
 }
 terminate(){if(this.stopped)return;this.stopped=true;if(this.generation)void this.request('process.terminate',{}).catch(()=>{});}
 async waitForExit(signal){try{await Promise.race([this.done.catch(()=>{}),...(signal?[new Promise(resolve=>{if(signal.aborted)resolve();else signal.addEventListener('abort',resolve,{once:true});})]:[])]);return this.empty;}catch{return false;}}
}
