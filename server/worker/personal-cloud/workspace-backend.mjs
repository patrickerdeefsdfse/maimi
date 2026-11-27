// Candidate backend factory only. Production registration/auth transport is not enabled.
import {randomUUID} from 'node:crypto';
const clone=value=>JSON.parse(JSON.stringify(value));
const fail=code=>{throw new Error(code);};
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
function document(value){
 if(!value||Array.isArray(value)||typeof value!=='object')fail('WORKSPACE_SNAPSHOT_INVALID');
 if(!Object.keys(value).length)return {tables:{workspaces:{}},global:null};
 if(!same(Object.keys(value).sort(),['global','tables'])||!value.tables||!same(Object.keys(value.tables),['workspaces'])||!value.tables.workspaces||Array.isArray(value.tables.workspaces)||typeof value.tables.workspaces!=='object')fail('WORKSPACE_SNAPSHOT_INVALID');
 return clone(value);
}
function revision(value){if(typeof value!=='string'||!/^\d+$/.test(value)||BigInt(value)>=2n**63n)fail('WORKSPACE_REVISION_INVALID');return value;}
function strictJSON(value){
 const seen=new Set();
 function visit(v){
  if(v===null||typeof v==='string'||typeof v==='boolean')return;
  if(typeof v==='number'&&Number.isFinite(v))return;
  if(typeof v!=='object'||seen.has(v)||(!Array.isArray(v)&&Object.getPrototypeOf(v)!==Object.prototype&&Object.getPrototypeOf(v)!==null))fail('WORKSPACE_VALUE_INVALID');
  // Native rc5 global mutations carry optional properties with undefined.
  // Match the shipped JSON backend: omit those object keys, never coerce
  // non-finite numbers or silently turn an undefined array entry into null.
  seen.add(v);for(const x of Object.values(v)){if(x===undefined&&!Array.isArray(v))continue;visit(x);}seen.delete(v);
 }
 visit(value);return clone(value);
}
export function createWorkspaceBackend(transport,{onFatal}){
 if(typeof transport?.load!=='function'||typeof transport?.compareAndSwap!=='function'||typeof onFatal!=='function')fail('WORKSPACE_BACKEND_CONFIGURATION_REQUIRED');
 let closed=false,closing=false,opened=false,unit=null,poison=null;
 function healthy(){if(poison)throw poison;if(closed)fail('WORKSPACE_BACKEND_CLOSED');}
 function fatal(error){
  if(!poison){poison=Error('WORKSPACE_STORAGE_UNCERTAIN_OR_FENCED',{cause:error});queueMicrotask(()=>{try{onFatal(poison);}catch{}});}
  return poison;
 }
 const backend={get failed(){return Boolean(poison);},kv:{async open(descriptor){
  healthy();if(closing)fail('WORKSPACE_BACKEND_CLOSED');if(opened)fail('WORKSPACE_BACKEND_ALREADY_OPEN');
  const {name,version,tables,hasGlobal,layout='single',...extra}=descriptor;
  if(name!=='workspace'||version!==2||!same(tables,['workspaces'])||hasGlobal!==true||layout!=='single'||Object.keys(extra).length)fail('WORKSPACE_DESCRIPTOR_REFUSED');
  opened=true;let snapshot,rev,unitClosed=false,unitClosing=false,tail=Promise.resolve();
  try{const loaded=await transport.load();rev=revision(loaded.revision);snapshot=document(loaded.value);}catch(error){throw fatal(error);}
  function live(){healthy();if(unitClosed)fail('WORKSPACE_UNIT_CLOSED');}
  function enqueue(change){
   if(unitClosing)fail('WORKSPACE_UNIT_CLOSED');
   const operation=tail.then(async()=>{
    live();const next=clone(snapshot);change(next);const id=randomUUID();let answer;
    try{answer=await transport.compareAndSwap({expected_revision:rev,write_id:id,value:next});
     if(answer.acknowledged!==true||revision(answer.revision)!==(BigInt(rev)+1n).toString())fail('WORKSPACE_COMMIT_ACK_INVALID');
    }catch(error){throw fatal(error);}
    // Both backend memory and native DomainImpl advance only after PG commit.
    snapshot=next;rev=answer.revision;
   });
   tail=operation.catch(()=>{});return operation;
  }
  function key(table,key){if(table!=='workspaces'||typeof key!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$/.test(key)||['constructor','prototype','__proto__'].includes(key))fail('WORKSPACE_RECORD_KEY_INVALID');}
  unit={
   async loadAll(){live();return clone(snapshot);},
   async putRecord(table,id,value){key(table,id);const data=strictJSON(value);return enqueue(next=>{next.tables.workspaces[id]=data;});},
   async deleteRecord(table,id){key(table,id);return enqueue(next=>{delete next.tables.workspaces[id];});},
   async setGlobal(value){const data=strictJSON(value);return enqueue(next=>{next.global=data;});},
   async close(){unitClosing=true;await tail;unitClosed=true;},
  };
  return unit;
 }},async close(){closing=true;try{if(unit)await unit.close();}finally{closed=true;}}};
 return backend;
}
