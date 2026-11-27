/** Company boundary for the unmodified native desktop window/backend controller. */
import {join} from 'node:path'
import {pathToFileURL} from 'node:url'
import {app,shell,safeStorage} from 'electron'
import type {DesktopHostReady,DesktopQuitInspection} from './host-process.ts'

type AuthorizedDrag = {path:string;size:number;sha256:string} | null;
interface CompanySession extends DesktopHostReady {
 resolveDragResource(address:string,signal?:AbortSignal):Promise<AuthorizedDrag>
 stop():Promise<void>
 inspectQuit():Promise<DesktopQuitInspection>
 updateTasks(action:'inspect'|'lock'|'unlock'):Promise<boolean>
 hasCompanySession():boolean
 signOut():Promise<boolean>
 blockResume():Promise<void>
 deviceStatus():{remembered:boolean;storageAvailable:boolean;pendingRevocation:boolean}
}
interface CompanyAdapter {
 signInAndStartCompanyNativeSession(options:{runtimeRoot:string;stateRoot:string;safeStorage:typeof safeStorage;primaryRuntime?:string;packageManager?:{pnpm:string;nodeBin:string};signal:AbortSignal;onFailure:(error:Error)=>void;openBrowser:(url:string,details?:{verificationCode?:string})=>Promise<void>}):Promise<CompanySession>
}

/** Fixed at build time. This candidate never joins the official account/update channel. */
export const COMPANY_CANDIDATE=true

export class CompanyNativeDesktopHost {
 private operation:Promise<CompanySession>|undefined
 private active:CompanySession|undefined
 private readonly abort=new AbortController()
 constructor(private readonly runtimeRoot:string,private readonly onFailure:(error:Error)=>void,private readonly confirmBrowser:(code:string,signal:AbortSignal)=>Promise<boolean>,private readonly primaryRuntime?:string,private readonly packageManager?:{pnpm:string;nodeBin:string}){}
 async start():Promise<DesktopHostReady>{
  const entry=pathToFileURL(join(app.getAppPath(),'maimi','company-desktop-session.mjs')).href
  const adapter=await import(entry) as CompanyAdapter
  this.operation??=adapter.signInAndStartCompanyNativeSession({
   runtimeRoot:this.runtimeRoot,stateRoot:join(app.getPath('userData'),'native-company-state'),safeStorage,...(this.primaryRuntime===undefined?{}:{primaryRuntime:this.primaryRuntime}),...(this.packageManager===undefined?{}:{packageManager:this.packageManager}),signal:this.abort.signal,onFailure:this.onFailure,
   openBrowser:async(url,details)=>{
    const parsed=new URL(url)
    const code=details?.verificationCode??''
    if(parsed.origin!=='https://desktop-gateway.example.invalid'||parsed.username||parsed.password||parsed.pathname!=='/v1/desktop/auth/authorize'||!/^[A-F0-9]{4}-[A-F0-9]{4}$/.test(code))throw Error('COMPANY_AUTH_URL_INVALID')
    if(!await this.confirmBrowser(code,this.abort.signal)){this.abort.abort();throw Error('COMPANY_LOGIN_CANCELLED')}
    this.abort.signal.throwIfAborted();await shell.openExternal(parsed.href)
   },
  })
  this.active=await this.operation
  this.abort.signal.throwIfAborted()
  return {url:this.active.url,injections:this.active.injections}
 }
 async stop(_requireCleanStop=false):Promise<void>{this.abort.abort();const session=await this.operation?.catch(()=>undefined);await session?.stop();this.active=undefined}
 async blockResume():Promise<void>{const session=await this.operation;if(!session)throw Error('COMPANY_SESSION_NOT_READY');await session.blockResume()}
 async signOut():Promise<boolean>{this.abort.abort();const session=await this.operation?.catch(()=>undefined);try{return session?await session.signOut():true}finally{this.active=undefined}}
 async resolveDragResource(address:string,signal?:AbortSignal):Promise<AuthorizedDrag>{
  if(!this.active)throw Error('COMPANY_HOST_NOT_READY');
  return this.active.resolveDragResource(address,signal);
 }
 async inspectQuit():Promise<DesktopQuitInspection>{if(!this.active)throw Error('COMPANY_HOST_NOT_READY');return this.active.inspectQuit()}
 async updateTasks(action:'inspect'|'lock'|'unlock'):Promise<boolean>{if(!this.active)throw Error('COMPANY_HOST_NOT_READY');return this.active.updateTasks(action)}
}
