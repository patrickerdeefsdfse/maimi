'use strict';
// Product window only: no credentials, runtime paths, or generic IPC surface.
const {contextBridge,ipcRenderer}=require('electron');
const arg=process.argv.find(value=>value.startsWith('--megmeet-native-origin='));
const expected=arg?.slice('--megmeet-native-origin='.length);
if(process.isMainFrame&&typeof expected==='string'&&/^http:\/\/127\.0\.0\.1:\d+$/.test(expected)&&location.origin===expected){
  contextBridge.exposeInMainWorld('megmeetDesktop', Object.freeze({openUpdates:()=>ipcRenderer.invoke('desktop:open-updates')}));
  const channels={input:'dsh-desktop:shortcuts-input',close:'dsh-desktop:shortcuts-close-window',get:'dsh-desktop:shortcuts-get',edit:'dsh-desktop:shortcuts-edit',changed:'dsh-desktop:shortcuts-changed',recording:'dsh-desktop:shortcuts-recording'};
  contextBridge.exposeInMainWorld('dshDesktop',{protocolVersion:1,
    keyboard:{closeWindow:revision=>ipcRenderer.invoke(channels.close,revision),subscribe(listener){
      if(typeof listener!=='function')throw TypeError('Listener required');
      const receive=(_event,input)=>{
        if(input?.kind==='webview')return;
        if(input?.kind==='iframe'){
          const element=document.activeElement;
          if(!(element instanceof HTMLIFrameElement)||!element.isConnected||!element.matches('iframe[data-sidebar-browser-frame], iframe[data-html-preview]')||!input.frameName||element.name!==input.frameName)return;
        }
        listener(input);
      };ipcRenderer.on(channels.input,receive);return()=>ipcRenderer.off(channels.input,receive);
    }},
    shortcuts:{get:definitions=>ipcRenderer.invoke(channels.get,definitions),edit:(edit,revision)=>ipcRenderer.invoke(channels.edit,edit,revision),recording:active=>ipcRenderer.invoke(channels.recording,active),subscribe(listener){
      if(typeof listener!=='function')throw TypeError('Listener required');
      const receive=(_event,value)=>listener(value);ipcRenderer.on(channels.changed,receive);return()=>ipcRenderer.off(channels.changed,receive);
    }}
  });
  const mark=()=>{document.documentElement.dataset.platform=process.platform;};
  if(document.documentElement)mark();else window.addEventListener('DOMContentLoaded',mark,{once:true});
}
