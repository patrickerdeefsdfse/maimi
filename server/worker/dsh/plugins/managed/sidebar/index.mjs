import * as upstream from './upstream.mjs';
import {sidebarFacade,surfaceOf} from '../policy.mjs';
export const name='dsh-better-sidebar';
export const inject=['webServer','sessions','connection','tools'];
export function apply(ctx,config={}) {
 if(surfaceOf(config)!=='desktop')throw Error('BETTER_SIDEBAR_DESKTOP_ONLY');
 upstream.apply(sidebarFacade(ctx,config),{...config,htmlViewerNoSandbox:false,htmlViewerDefaultUnsafe:false,agentOpenTools:false});
}
