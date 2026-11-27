import * as upstream from './upstream.mjs';
import {contextFacade,surfaceOf} from '../policy.mjs';
export const name='dsh-context';
export const inject=['sessionProjections','connection'];
export function apply(ctx,config={}) {
 surfaceOf(config);
 upstream.apply(contextFacade(ctx,config),{...config,promptPrefix:'',promptSuffix:''});
 // The enterprise model is billed by the company gateway. No public account
 // credentials are looked up and no external balance request is issued.
 ctx.effect(()=>ctx.connection.fetch.register({path:'/api/dsh-context/balance',methods:['POST'],requestBody:'buffered',fetch:()=>Response.json({ok:true,value:null,managedBy:'maimi-company'},{headers:{'cache-control':'no-store'}})}),'maimi: company model balance');
}
