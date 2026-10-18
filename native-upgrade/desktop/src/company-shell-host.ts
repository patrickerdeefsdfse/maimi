/** Company boundary for the unmodified native desktop window/backend controller. */
import {join} from 'node:path'
import {pathToFileURL} from 'node:url'
import {app,dialog,shell} from 'electron'
import type {DesktopHostReady,DesktopQuitInspection} from './host-process.ts'

interface CompanySession extends DesktopHostReady {
 stop():Promise<void>
 inspectQuit():Promise<DesktopQuitInspection>
 updateTasks(action:'inspect'|'lock'|'unlock'):Promise<boolean>
 hasCompanySession():boolean
}
interface CompanyAdapter {
 signInAndStartCompanyNativeSession(options:{runtimeRoot:string;stateRoot:string;primaryRuntime?:string;packageManager?:{pnpm:string;nodeBin:string};signal:AbortSignal;onFailure:(error:Error)=>void;openBrowser:(url:string,details?:{verificationCode?:string})=>Promise<void>}):Promise<CompanySession>
}

/** Fixed at build time. This candidate never joins the official account/update channel. */
export const COMPANY_CANDIDATE=true

export class CompanyNativeDesktopHost {
 private operation:Promise<CompanySession>|undefined
 private active:CompanySession|undefined
 private readonly abort=new AbortController()
 constructor(private readonly runtimeRoot:string,private readonly onFailure:(error:Error)=>void,private readonly primaryRuntime?:string,private readonly packageManager?:{pnpm:string;nodeBin:string}){}
 async start():Promise<DesktopHostReady>{
  const entry=pathToFileURL(join(app.getAppPath(),'maimi','company-desktop-session.mjs')).href
  const adapter=await import(entry) as CompanyAdapter
  this.operation??=adapter.signInAndStartCompanyNativeSession({
   runtimeRoot:this.runtimeRoot,stateRoot:join(app.getPath('userData'),'native-company-state'),...(this.primaryRuntime===undefined?{}:{primaryRuntime:this.primaryRuntime}),...(this.packageManager===undefined?{}:{packageManager:this.packageManager}),signal:this.abort.signal,onFailure:this.onFailure,
   openBrowser:async(url,details)=>{
    const parsed=new URL(url)
    if(parsed.origin!=='https://platform-ai.megmeet.com'||parsed.pathname!=='/v1/desktop/auth/authorize'||!/^\w{4}-\w{4}$/.test(details?.verificationCode??''))throw Error('COMPANY_AUTH_URL_INVALID')
    const result=await dialog.showMessageBox({type:'info',title:'麦米公司登录',message:'请在公司登录页面确认相同的验证码',detail:details?.verificationCode??'',buttons:['打开公司登录','取消'],defaultId:0,cancelId:1})
    if(result.response!==0){this.abort.abort();throw Error('COMPANY_LOGIN_CANCELLED')}
    this.abort.signal.throwIfAborted();await shell.openExternal(parsed.href)
   },
  })
  this.active=await this.operation
  this.abort.signal.throwIfAborted()
  return {url:this.active.url,injections:this.active.injections}
 }
 async stop(_requireCleanStop=false):Promise<void>{this.abort.abort();const session=await this.operation?.catch(()=>undefined);await session?.stop();this.active=undefined}
 async inspectQuit():Promise<DesktopQuitInspection>{if(!this.active)throw Error('COMPANY_HOST_NOT_READY');return this.active.inspectQuit()}
 async updateTasks(action:'inspect'|'lock'|'unlock'):Promise<boolean>{if(!this.active)throw Error('COMPANY_HOST_NOT_READY');return this.active.updateTasks(action)}
}
