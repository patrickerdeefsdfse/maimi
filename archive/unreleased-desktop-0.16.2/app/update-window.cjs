'use strict';
const path=require('node:path');
const {pathToFileURL}=require('node:url');
function createUpdateWindow({electron,icon,preferences,snapshot,checkNow,installNow,parent}) {
  const {BrowserWindow,ipcMain}=electron;
  const url=pathToFileURL(path.join(__dirname,'update.html')).href;
  let window;
  function trusted(event) {
    if(!window||window.isDestroyed()||event.sender!==window.webContents||event.senderFrame!==window.webContents.mainFrame||event.senderFrame.url!==url)throw Error('Untrusted software-update window');
  }
  const actions={status:()=>snapshot(),check:()=>{
    // Trigger the real check without making renderer IPC wait for its download.
    Promise.resolve(checkNow()).catch(()=>{});return snapshot();
  },install:async()=>installNow(window)};
  for(const [key,action] of Object.entries(actions))ipcMain.handle('software-update:'+key,async(event,...args)=>{trusted(event);if(args.length)throw Error('Update arguments are not accepted');return action();});
  function open() {
    if(window&&!window.isDestroyed()){window.show();window.focus();return;}
    window=new BrowserWindow({width:530,height:540,minWidth:460,minHeight:450,resizable:true,show:false,title:'麦米 · 软件更新',icon,
      parent:parent?.(),webPreferences:{...preferences,preload:path.join(__dirname,'preload-update.cjs'),partition:'megmeet-update-local'}});
    window.webContents.setWindowOpenHandler(()=>({action:'deny'}));
    window.webContents.on('will-navigate',(event,target)=>{if(target!==url)event.preventDefault();});
    window.webContents.on('will-redirect',event=>event.preventDefault());
    window.once('ready-to-show',()=>{window?.show();});
    window.on('closed',()=>{window=undefined;});
    void window.loadURL(url);
  }
  return {open,get window(){return window;}};
}
function nativeUpdateHandler({getWindow,getHost,isNativeUrl,open}) {
  return (event,...args)=>{
    const native=getWindow(),host=getHost();
    if(args.length||!native||native.isDestroyed()||event.sender!==native.webContents||event.senderFrame!==native.webContents.mainFrame||!host||!isNativeUrl(event.senderFrame?.url,host.origin))throw Error('Untrusted desktop update request');
    return open();
  };
}
module.exports={createUpdateWindow,nativeUpdateHandler};
