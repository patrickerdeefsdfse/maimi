// A native DSH provider composed with the unmodified official class. Neither
// the renderer nor a model tool can issue a private task ticket through this.
import {captureToolScope,acquireToolTicket,releaseToolTicket} from './native-task-context.mjs';
import {installEnterpriseSearch} from './native-search-install.mjs';
import {createNativeSearchConnection} from './native-search-connection.mjs';
export const name='megmeet-official-first-search';
export const inject=['agents','web'];
export async function apply(ctx,config){
 if(config.endpoint!=='http://127.0.0.1:3086/__megmeet/search'||!/^[A-Za-z0-9_-]{43}$/.test(config.token||''))throw Error('SEARCH_PRIVATE_ENDPOINT_REQUIRED');
 const {DeepSeekSearchProvider}=await import('file:///opt/dsh/node_modules/@deepseek-ai/dsh-web-search-deepseek/lib/index.js');
 const connection=createNativeSearchConnection({endpoint:config.endpoint,token:config.token,acquireToolTicket,releaseToolTicket});
 installEnterpriseSearch(ctx,{DeepSeekSearchProvider,nativeScopes:{captureToolScope},endpoint:config.endpoint,
  ...connection});
}
