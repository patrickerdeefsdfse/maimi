// Native-process singleton, shared by reviewed tools and the task controller.
let scopes,control;
export function bindNativeTaskContext(value,request){
 if(scopes)throw Error('CLOUD_NATIVE_CONTEXT_ALREADY_BOUND');scopes=value;control=request;
}
export function captureToolScope(agent){if(!scopes)throw Error('CLOUD_NATIVE_CONTEXT_REQUIRED');return scopes.captureToolScope(agent);}
export async function acquireToolTicket(scope,purpose){
 if(!control)throw Error('CLOUD_NATIVE_CONTEXT_REQUIRED');
 scopes.validateToolScope(scope);
 const value=await control('issue',{kind:'tool',scope:{...scope,purpose}});
 if(!/^mtt_[A-Za-z0-9_-]{43}$/.test(value.token||''))throw Error('CLOUD_NATIVE_TICKET_INVALID');return value.token;
}
export async function releaseToolTicket(token){if(control)await control('release',{token});}
export function resolveModelCredential(){if(!scopes)throw Error('CLOUD_NATIVE_CONTEXT_REQUIRED');return scopes.resolveCredential();}
