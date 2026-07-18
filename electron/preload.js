// Preload script — safe bridge between renderer and main.
// Exposes a small, explicit API as window.coachApi.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('coachApi', {
  getBackendPort: () => ipcRenderer.invoke('get-backend-port'),
  pickFilesToUpload: (coachName) => ipcRenderer.invoke('pick-file-and-upload', { coachName }),
  readFile: (filePath) => ipcRenderer.invoke('read-file', filePath),
  onBackendExit: (cb) => ipcRenderer.on('backend-exited', (_evt, code) => cb(code)),
});
