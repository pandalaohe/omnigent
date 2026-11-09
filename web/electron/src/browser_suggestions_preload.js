"use strict";
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("places", {
  data: () => ipcRenderer.invoke("omnigent:browser-suggestions-data"),
  choose: (index) => ipcRenderer.send("omnigent:browser-suggestions-choose", index),
  onData: (callback) => ipcRenderer.on("browser-suggestions", (_event, data) => callback(data)),
});
