// Sticky Host admission fence. It never claims task completion or releases PG ownership.
export function createWorkspaceHealth({enabled,onFatal=()=>{}}){
 let failed=false;
 return {get failed(){return failed;},
  mark(){if(enabled&&!failed){failed=true;onFatal();}},
  message(value){if(value?.type==='maimi-workspace-state-fatal'&&value.protocol===1&&value.reason==='workspace_state_uncertain_or_fenced')this.mark();},
  assert(){if(failed)throw Object.assign(Error('工作区状态存储暂不可用，请保留当前任务并稍后重新进入。'),{status:503,nativeCode:'session/workspace-storage-unavailable'});},
  refuse(res){if(!failed)return false;res.writeHead(503,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({detail:'workspace_state_uncertain_or_fenced'}));return true;}
 };
}
