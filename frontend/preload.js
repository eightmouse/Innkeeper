const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  sendToPython:   (cmd) => ipcRenderer.send('to-python', cmd),
  windowClose:    ()    => ipcRenderer.send('window-close'),
  windowMinimize: ()    => ipcRenderer.send('window-minimize'),
  windowMaximize: ()    => ipcRenderer.send('window-maximize'),
  openExternal:   (url) => ipcRenderer.send('open-external', url),
  setResolution:  (w, h) => ipcRenderer.send('set-resolution', w, h),
  showDataFolder: ()     => ipcRenderer.send('show-data-folder'),
  onFromPython:   (cb)  => ipcRenderer.on('from-python', (_event, data) => cb(data)),
});
