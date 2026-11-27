// Shared native implementation. Cloud/desktop entry points separately pin
// their loopback endpoint; this function never handles real provider keys.
export function createNativeSearchConnection({endpoint,token,acquireToolTicket,releaseToolTicket,fetchImpl=fetch}){
 const url=new URL(endpoint);
 if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||!url.port||url.pathname!=='/__megmeet/search'||url.search||url.hash||url.username||url.password||!/^[A-Za-z0-9_-]{43}$/.test(token||''))throw Error('SEARCH_PRIVATE_ENDPOINT_REQUIRED');
 const control=async(action,ticket)=>{
  const response=await fetchImpl(endpoint+'/'+action,{method:'POST',redirect:'error',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({ticket}),signal:AbortSignal.timeout(10000)});
  let bytes=0;const chunks=[];for await(const chunk of response.body||[]){bytes+=chunk.byteLength;if(bytes>8192)throw Error('SEARCH_SCOPE_REVOKED');chunks.push(Buffer.from(chunk));}
  let value;try{value=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw Error('SEARCH_SCOPE_REVOKED');}
  if(!response.ok)throw Object.assign(Error('SEARCH_SCOPE_REVOKED'),{code:'SEARCH_SCOPE_REVOKED'});return value;
 };
 return{
  issueCredential:async scope=>{const native=await acquireToolTicket(scope,'web-search');let transferred=false;try{const value=await control('issue',native);if(!/^mst_[A-Za-z0-9_-]{43}$/.test(value.ticket||''))throw Error('SEARCH_SCOPE_REVOKED');transferred=true;return value.ticket;}finally{if(!transferred)await releaseToolTicket(native);}},
  releaseCredential:async ticket=>{try{await control('release',ticket);}catch{/* Task revocation/expiry still release its hold in the host. */}}
 };
}
