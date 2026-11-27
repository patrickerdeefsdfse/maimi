import * as upstream from './upstream.mjs';
import {surfaceOf} from '../policy.mjs';
export const name='@changfenhuang/dsh-genui';
export const inject=['systemPrompt'];
const guidance='When a chart, table or small interactive explanation materially helps, use a dsh-ui JSON fence. Plain text remains appropriate for simple replies. Use validate_dsh_ui for complex specs. Do not ask for passwords or secrets. Use local sorting/filtering before model actions. Do not embed external images, audio or video. UI form state is temporary in memory; history is the canonical record. Export only when the user asks.';
export function apply(ctx,config={}){
 surfaceOf(config);
 const prompt=new Proxy(ctx.systemPrompt,{get(target,key){if(key==='section')return value=>target.section({...value,text:guidance});const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
 const facade=new Proxy(ctx,{get(target,key){if(key==='systemPrompt')return prompt;const value=Reflect.get(target,key,target);return typeof value==='function'?value.bind(target):value;}});
 upstream.apply(facade,{fenceFeedback:false,renderUiTool:true});
}
