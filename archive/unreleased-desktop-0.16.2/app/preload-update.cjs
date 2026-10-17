'use strict';
const {contextBridge,ipcRenderer}=require('electron');
if(process.isMainFrame)contextBridge.exposeInMainWorld('megmeetUpdate',Object.freeze({status:()=>ipcRenderer.invoke('software-update:status'),check:()=>ipcRenderer.invoke('software-update:check'),install:()=>ipcRenderer.invoke('software-update:install')}));
