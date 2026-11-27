// Diagnostic projection only: never returns request data or native error text.
const METHODS=new Set(['workspace/create','workspace/rename','settings/update','settings/set','models/list']);
const CODES=new Set(['gateway/internal','gateway/invalid-request','gateway/not-found','gateway/forbidden','gateway/unauthorized','WORKSPACE_REQUEST_REFUSED','WORKSPACE_DESCRIPTOR_REFUSED','WORKSPACE_RESPONSE_LIMIT','WORKSPACE_STORAGE_UNCERTAIN_OR_FENCED','WORKSPACE_GENERATION_NOT_ADMITTED','WORKSPACE_RECORD_KEY_INVALID','WORKSPACE_VALUE_INVALID','WORKSPACE_SNAPSHOT_INVALID','WORKSPACE_FORMAT_MISMATCH','personal_cloud_manager_configuration','personal_cloud_unavailable']);
export function nativeBootstrapSummary(method,http,body){
 const error=body&&typeof body==='object'&&body.result&&typeof body.result==='object'?body.result.error:null;
 const supplied=error&&typeof error==='object'?error.code:undefined;
 return {event:'NATIVE_BOOTSTRAP_RPC_REFUSED',method:METHODS.has(method)?method:'OTHER_BOOTSTRAP_RPC',http:Number.isInteger(http)&&http>=100&&http<=599?http:null,result_ok:body?.result?.ok===true,error_code:CODES.has(supplied)?supplied:'UNCLASSIFIED'};
}
