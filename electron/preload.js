'use strict'
const { contextBridge, ipcRenderer } = require('electron')

// Expose safe APIs to the renderer
contextBridge.exposeInMainWorld('queryLuxDesktop', {
  version:    () => ipcRenderer.invoke('get-version'),
  platform:   () => process.platform,
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
})
