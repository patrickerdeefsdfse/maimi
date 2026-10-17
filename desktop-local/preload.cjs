'use strict';
const { contextBridge, ipcRenderer } = require('electron');
// Main verifies the exact packaged entry page for every invocation. The DSH
// renderer receives no token, filesystem primitive, command or arbitrary URL.
contextBridge.exposeInMainWorld('megmeetEntry', Object.freeze({
  updates: () => ipcRenderer.invoke('entry:updates'),
  status: () => ipcRenderer.invoke('entry:status'),
  login: () => ipcRenderer.invoke('entry:login'),
  local: () => ipcRenderer.invoke('entry:local'),
  logout: () => ipcRenderer.invoke('entry:logout'),
}));
