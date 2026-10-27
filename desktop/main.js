// life-os desktop: a window onto the server (over Tailscale) plus a ⌘⇧Space quick-capture box.
// All app logic stays on the server, so this is the same app as the iPhone PWA.
const { app, BrowserWindow, globalShortcut, Menu, Tray, nativeImage, nativeTheme, shell, screen, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");

const SERVER = process.env.LIFEOS_URL || "https://lifeos.tail969156.ts.net";
const CAPTURE_SHORTCUT = "CommandOrControl+Shift+Space";
const statePath = () => path.join(app.getPath("userData"), "window.json");

let main, capture, tray;

function loadState() {
  try { return JSON.parse(fs.readFileSync(statePath(), "utf8")); } catch { return { width: 1100, height: 820 }; }
}

function saveState() {
  if (!main || main.isDestroyed()) return;
  fs.writeFileSync(statePath(), JSON.stringify(main.getBounds()));
}

function isOurs(url) {
  return url.startsWith(SERVER);
}

// Show a friendly page (with a retry button) when the server can't be reached, e.g. Tailscale is off.
function guardLoad(win, url) {
  win.webContents.on("did-fail-load", (_e, code, _desc, failedUrl, isMainFrame) => {
    if (!isMainFrame || code === -3) return; // -3: aborted (a normal navigation replaced it)
    win.loadFile(path.join(__dirname, "offline.html"), { query: { retry: failedUrl || url } });
  });
  win.loadURL(url);
}

function openExternally(win) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (e, url) => {
    if (!isOurs(url) && !url.startsWith("file://")) { e.preventDefault(); shell.openExternal(url); }
  });
}

function createMain() {
  const state = loadState();
  main = new BrowserWindow({
    ...state, minWidth: 380, minHeight: 500, show: false,
    titleBarStyle: "hiddenInset", trafficLightPosition: { x: 14, y: 12 },
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#0e0f15" : "#f4f5f8", // the page's paper color: no flash
    webPreferences: { preload: path.join(__dirname, "preload.js") },
  });
  openExternally(main);
  main.once("ready-to-show", () => main.show());
  main.on("close", saveState);
  main.on("closed", () => { main = null; });
  guardLoad(main, SERVER + "/");
}

function showMain() {
  if (!main) createMain(); else { main.show(); main.focus(); }
}

function toggleCapture() {
  if (capture && capture.isVisible()) { capture.hide(); return; }
  if (!capture) {
    capture = new BrowserWindow({
      width: 560, height: 170, frame: false, resizable: false, show: false, alwaysOnTop: true,
      fullscreenable: false, skipTaskbar: true, vibrancy: "popover", visualEffectState: "active",
      roundedCorners: true, webPreferences: { preload: path.join(__dirname, "preload.js") },
    });
    openExternally(capture);
    capture.on("blur", () => capture.hide());
    guardLoad(capture, SERVER + "/capture");
  } else {
    capture.loadURL(SERVER + "/capture"); // fresh, empty box each time
  }
  // Open on the screen the mouse is on, a bit above center (like Spotlight).
  const { workArea } = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
  const [w, h] = capture.getSize();
  capture.setPosition(Math.round(workArea.x + (workArea.width - w) / 2), Math.round(workArea.y + workArea.height * 0.28));
  capture.show();
  capture.focus();
}

ipcMain.on("hide-capture", () => capture?.hide());
ipcMain.on("retry", (e, url) => BrowserWindow.fromWebContents(e.sender)?.loadURL(url || SERVER + "/"));

app.whenReady().then(() => {
  createMain();
  globalShortcut.register(CAPTURE_SHORTCUT, toggleCapture);

  const icon = nativeImage.createFromPath(path.join(__dirname, "trayTemplate.png"));
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  if (icon.isEmpty()) tray.setTitle("◐");
  tray.setToolTip("life-os");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open life-os", click: showMain },
    { label: "Quick capture", accelerator: CAPTURE_SHORTCUT, click: toggleCapture },
    { type: "separator" },
    { label: "Quit", role: "quit" },
  ]));

  app.on("activate", showMain);
});

app.on("will-quit", () => globalShortcut.unregisterAll());
// Keep running in the menu bar (for the shortcut) when the main window is closed.
app.on("window-all-closed", (e) => e.preventDefault?.());
