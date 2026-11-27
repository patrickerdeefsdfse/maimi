import {publicCatalog,surfaceOf,installPluginManagerFence} from './policy.mjs';
export const name='maimi-managed-plugins';
export const inject=['connection'];
export function apply(ctx,config={}) {
 const surface=surfaceOf(config);
 installPluginManagerFence(ctx);
 ctx.effect(()=>ctx.connection.fetch.register({path:'/api/maimi/plugins/catalog',methods:['POST'],requestBody:'buffered',fetch:()=>Response.json(publicCatalog(surface),{headers:{'cache-control':'no-store'}})}),'maimi: reviewed plugin catalog');
 if(surface==='web')ctx.effect(()=>ctx.connection.fetch.register({path:'/api/megmeet-cloud/provider-readiness',methods:['GET'],requestBody:'buffered',fetch:async request=>{
  if(request.headers.has('origin')||request.headers.has('sec-fetch-site'))return new Response(null,{status:403});
  let stage='provider_imports';
  try{
   const [{PgSessionPersistence},{ManagedAttachmentStore},{default:CompanyWorkspaceController},{managedProviderReadiness}]=await Promise.all([
    import('/opt/personal-cloud/native-history-plugin.mjs'),import('/opt/personal-cloud/native-attachment-plugin.mjs'),import('/opt/personal-cloud/company-workspace-controller.mjs'),import('/opt/personal-cloud/provider-readiness.mjs')]);
   const persistence=ctx.get('sessionPersistence'),attachments=ctx.get('attachments'),workspace=ctx.get('workspaceController');
   stage='history_provider';if(!(persistence instanceof PgSessionPersistence))throw Error('ENTERPRISE_PROVIDER_NOT_ACTIVE');
   stage='attachment_provider';if(!(attachments instanceof ManagedAttachmentStore))throw Error('ENTERPRISE_PROVIDER_NOT_ACTIVE');
   stage='workspace_provider';if(!(workspace instanceof CompanyWorkspaceController))throw Error('ENTERPRISE_PROVIDER_NOT_ACTIVE');
   // Read-only, current-owner PG roundtrip. No session creation and no claim
   // that a backend mount or an end-user storage write has passed acceptance.
   stage='postgres_read';await persistence.stat('session-00000000-0000-0000-0000-000000000000',{signal:AbortSignal.timeout(10000)});
   return Response.json(managedProviderReadiness({pgReadVerified:true}),{headers:{'cache-control':'no-store'}});
  }catch{console.error('MAIMI_PROVIDER_READINESS_FAILED',stage);return Response.json({ready:false,detail:'enterprise_provider_unavailable',stage},{status:503,headers:{'cache-control':'no-store'}});}
 }}),'maimi: enterprise provider readiness');
}
