const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("kiroclaw", {
  platform: process.platform,
  isElectron: true,
});

contextBridge.exposeInMainWorld("electronAPI", {
  onStatus: (cb) => {
    const handler = (_e, msg) => cb(msg);
    ipcRenderer.on("status", handler);
    return () => ipcRenderer.removeListener("status", handler);
  },
});
