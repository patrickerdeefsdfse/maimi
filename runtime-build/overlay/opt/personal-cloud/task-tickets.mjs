import {randomBytes} from 'node:crypto';
const SID=/^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const deny=()=>{throw Error('CLOUD_NATIVE_SCOPE_REQUIRED');};
const purposes={model:new Set(['conversation','compaction','session-title']),tool:new Set(['web-search','web-read','office','connector'])};
// Private worker/native channel only. Tickets never enter renderer or history.
export class TaskTickets {
 constructor({authority,now=Date.now}){this.authority=authority;this.now=now;this.pending=new Map();this.active=new Map();}
 issue(scope,kind){
  this.expire();
  if(!scope||Object.keys(scope).sort().join(',')!=='generation,purpose,rootSessionId,sessionId'
   ||!SID.test(scope.sessionId||'')||!purposes[kind]?.has(scope.purpose))deny();
  const task=this.authority.resolveScope(scope);
  if(this.pending.size+this.active.size>=128)deny();
  const token=(kind==='model'?'mmt_':'mtt_')+randomBytes(32).toString('base64url');
  const end=this.authority.hold(task,'tickets');
  this.pending.set(token,{task,scope:Object.freeze({...scope}),kind,end,expires:this.now()+30000});return token;
 }
 consume(token,kind,purpose){
  this.expire();const p=this.pending.get(token);
  if(!p||p.kind!==kind||(purpose&&p.scope.purpose!==purpose))deny();
  this.pending.delete(token);
  if(!this.authority.has(p.task)||p.task.revoked){p.end();deny();}
  const controller=new AbortController();let done=false;
  const finish=()=>{if(done)return;done=true;this.active.delete(token);p.end();};
  this.active.set(token,{task:p.task,controller,finish});
  return {task:p.task,scope:p.scope,signal:controller.signal,finish,end:finish};
 }
 release(token){const p=this.pending.get(token);if(p){this.pending.delete(token);p.end();}
  const a=this.active.get(token);if(a){a.controller.abort();a.finish();}}
 expire(){for(const [token,p]of this.pending)if(p.expires<=this.now())this.release(token);}
 revoke(task){for(const [token,p]of [...this.pending,...this.active])if(p.task===task)this.release(token);}
}
