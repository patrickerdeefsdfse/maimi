import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {createRememberedStore}=require('./company-auth/remembered-store.cjs');
const {createDeviceSession}=require('./company-auth/device-session.cjs');

/** Main process only. Electron safeStorage never crosses the Host or renderer boundary.
 * A normal app close keeps the device proof; explicit sign-out revokes/blocks it.
 */
export function createCompanyDeviceLogin({directory,safeStorage,api,platform=process.platform}) {
 const store=createRememberedStore({directory,safeStorage,platform});
 const device=createDeviceSession({store,api});
 let stopped=false;
 return {
  status:()=>device.status(),
  blockResume(){store.block();device.cancel();},
  async authenticate({openBrowser,signal,onStatus=()=>{}}) {
   if(stopped)throw Error('COMPANY_DEVICE_LOGIN_STOPPED');
   signal?.throwIfAborted();
   // No plaintext fallback. If OS encryption is temporarily unavailable, preserve
   // the encrypted proof and use a one-session login without enrolling a device.
   if(!store.available()) {
    onStatus('安全存储暂不可用，本次使用公司网页登录。');
    return api.signIn(openBrowser,{signal,onStatus});
   }
   onStatus('正在恢复公司登录…');
   // Network/ambiguous failures deliberately propagate: do not replace a proof
   // whose rotation may already have committed on the server.
   const result=await device.restore({signal});
   signal?.throwIfAborted();
   if(result.status==='restored')return result.auth;
   if(!['absent','expired','denied'].includes(result.status))throw Error('COMPANY_DEVICE_RESTORE_INVALID');
   onStatus('请通过公司浏览器登录。');
   const login=await device.login(openBrowser,{signal,onStatus});
   return login.auth;
  },
  async signOut(auth) {
   // The persisted signed-out marker is written before any remote request.
   return device.forget(auth);
  },
  async stop(){stopped=true;await device.stop();},
 };
}
