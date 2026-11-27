import * as upstream from './upstream.mjs';
import path from 'node:path';
import {surfaceOf} from '../policy.mjs';
export const name='maimi-browser-skill';
export const inject=['connection'];
export function apply(ctx,config={}){
 if(surfaceOf(config)!=='desktop')throw Error('BROWSER_SKILL_DESKTOP_ONLY');
 const configured=config.enabled===true&&typeof config.bskPath==='string'&&path.isAbsolute(config.bskPath)&&config.paired===true;
 ctx.effect(()=>ctx.connection.fetch.register({path:'/api/maimi/plugins/browser-readiness',methods:['POST'],requestBody:'buffered',fetch:()=>Response.json({package:'@wxg-prc-cpg/browser-skill-dsh-plugin',version:'0.3.3',state:configured?'configured':'not-configured',reason:configured?null:'需要本机 BrowserSkill 程序及浏览器扩展配对',browser_actions_performed:false},{headers:{'cache-control':'no-store'}})}),'maimi: browser readiness');
 if(config.enabled===true&&!configured)throw Error('BROWSER_SKILL_PAIRING_REQUIRED');
 if(configured)upstream.apply(ctx,{bskPath:config.bskPath,sessionStateDirectory:config.sessionStateDirectory,defaultTimeoutMs:30000,maxSessions:2,observationEnabled:false,lazyTools:true});
}
