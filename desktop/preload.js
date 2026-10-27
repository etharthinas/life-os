// Runs in the server's pages inside the desktop app. Marks the page so CSS can make room for the
// macOS title bar, and exposes the two things pages may ask the shell to do.
const { contextBridge, ipcRenderer } = require("electron");

window.addEventListener("DOMContentLoaded", () => document.documentElement.classList.add("electron"));

contextBridge.exposeInMainWorld("lifeos", {
  hideCapture: () => ipcRenderer.send("hide-capture"),
  retry: (url) => ipcRenderer.send("retry", url),
});
