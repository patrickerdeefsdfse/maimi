// The native host confirms its adapters and PG read; only the gateway can
// qualify its own selected file backend. Neither side calls local storage NAS.
export function managedProviderReadiness({pgReadVerified}={}) {
 if(pgReadVerified!==true)throw Error('PG_PROVIDER_READ_REQUIRED');
 return {ready:true,history:'postgresql',attachments:'managed-attachments',storage_backend:'gateway-managed',
  workspace:'fixed-company',pg_read_verified:true,storage_mount_verified:false};
}

export function isManagedProviderReady(value) {
 return Boolean(value&&value.ready===true&&value.history==='postgresql'&&value.attachments==='managed-attachments'
  &&value.storage_backend==='gateway-managed'&&value.workspace==='fixed-company'
  &&value.pg_read_verified===true&&value.storage_mount_verified===false);
}
