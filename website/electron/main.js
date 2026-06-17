const { app, BaseWindow, BrowserWindow, WebContentsView, shell, dialog, Tray, Menu, nativeImage, nativeTheme, session, desktopCapturer, systemPreferences } = require("electron");
const Store = require("electron-store");
const fs = require("fs");
const os = require("os");
const { spawn, execFile } = require("child_process");
const path = require("path");
const http = require("http");
const { findKiroclawBin } = require("./find-bin");
const { createTokenRetryHandler } = require("./token-retry");
const { createDisplayMediaHandler } = require("./display-media");

// ── Persistent settings for remote tunnel mode ──

const {
  DEFAULT_REMOTE_BIN,
  buildRemoteTokenCommand,
  parseTokenFromStdout,
} = require("./remote-token");

const store = new Store({
  defaults: {
    remoteHost: "",                        // e.g. "myhost.corp.amazon.com"
    kiroclawBinPath: DEFAULT_REMOTE_BIN,   // full path for non-interactive SSH
    sshTimeoutMs: 20000,
  },
});

function resolvePort() {
  const raw = process.env.KIROCLAW_PORT;
  if (!raw) return 8765;
  const n = parseInt(raw, 10);
  if (isNaN(n) || n < 1 || n > 65535) {
    console.warn(`Invalid KIROCLAW_PORT="${raw}", falling back to 8765`);
    return 8765;
  }
  return n;
}

const PORT = resolvePort();
const BACKEND_URL = `http://localhost:${PORT}`;
const HEALTH_URL = `${BACKEND_URL}/api/status`;
const POLL_INTERVAL_MS = 500;
const MAX_WAIT_MS = 120_000; // 2 min max wait for backend
const KIROCLAW_HOME = process.env.KIROCLAW_HOME || path.join(os.homedir(), ".kiroclaw");
const TAB_BAR_HEIGHT = 28; // macOS native tab bar height in px

const { validateRemoteSettings } = require("./validation");
const { attachContextMenu } = require("./context-menu");

// Set app name for macOS menu bar and dock
app.name = "KiroClaw";

let mainWindow = null;
let tray = null;
let gatewayProcess = null;
let isQuitting = false;

// ── Backend lifecycle ──


function sendStatus(msg) {
  mainWindow?.webContents?.send("status", msg);
}

function startGateway() {
  return new Promise((resolve) => {
    sendStatus("Checking if gateway is running…");
    checkBackend()
      .then(() => { sendStatus("Gateway already running ✓"); resolve(true); })
      .catch(() => {
        // Ensure ~/.kiroclaw/ directory exists before starting gateway
        // (gateway generates .local_secret itself on startup via O_CREAT|O_TRUNC)
        const kiroclawDir = KIROCLAW_HOME;
        try {
          fs.mkdirSync(kiroclawDir, { recursive: true, mode: 0o700 });
        } catch (err) {
          console.warn("Failed to create kiroclaw dir:", err.message);
        }

        const bin = findKiroclawBin(fs, os, path, process.resourcesPath, __dirname);
        sendStatus("Starting gateway…");
        console.log(`Starting gateway: ${bin} gateway`);
        const { KIROCLAW_PORT: _ignored, ...cleanEnv } = process.env;
        gatewayProcess = spawn(bin, ["gateway", "--no-open"], {
          stdio: "ignore",
          detached: false,
          env: { ...cleanEnv, KIROCLAW_PROJECT_DIR: path.resolve(__dirname, "..") },
        });
        gatewayProcess.on("error", (err) => {
          console.error("Failed to start gateway:", err.message);
          sendStatus(`Gateway failed: ${err.message}`);
          resolve(false);
        });
        gatewayProcess.on("exit", (code) => {
          console.log(`Gateway exited with code ${code}`);
          gatewayProcess = null;
        });
        resolve(true);
      });
  });
}

function stopGateway() {
  if (gatewayProcess) {
    console.log("Stopping gateway...");
    gatewayProcess.kill("SIGTERM");
    gatewayProcess = null;
  }
}

// ── Remote tunnel token fetch ──

function fetchRemoteToken() {
  const host = store.get("remoteHost");
  const binPath = store.get("kiroclawBinPath");
  if (!host) return Promise.resolve("");
  const validationErr = validateRemoteSettings(host, binPath);
  if (validationErr) {
    console.error(`Refusing SSH token fetch: ${validationErr}`);
    return Promise.resolve("");
  }

  const remoteCmd = buildRemoteTokenCommand(binPath);
  const debugHint = binPath === DEFAULT_REMOTE_BIN ? "candidates" : `custom=${binPath}`;

  return new Promise((resolve) => {
    sendStatus("Fetching token from remote dev desktop…");
    console.log(`SSH token fetch: ssh ${host} (${debugHint})`);
    execFile("/usr/bin/ssh", [
      "-o", "ConnectTimeout=10",
      host,
      remoteCmd,
    ], { timeout: Math.max(store.get("sshTimeoutMs") || 20000, 5000) }, (err, stdout, stderr) => {
      if (err) {
        console.error("SSH token fetch failed:", err.message);
        if (stderr) console.error("SSH stderr:", stderr.trim().slice(0, 500));
        return resolve("");
      }
      resolve(parseTokenFromStdout(stdout));
    });
  });
}

function fetchLocalToken(backendUrl = BACKEND_URL) {
  try {
    const secret = fs.readFileSync(path.join(KIROCLAW_HOME, ".local_secret"), "utf8").trim();
    return new Promise((resolve) => {
      const req = http.get(`${backendUrl}/api/token/local`, { headers: { "X-Local-Secret": secret }, timeout: 5000 }, (res) => {
        if (res.statusCode !== 200) { res.resume(); return resolve(""); }
        let data = "";
        res.on("error", () => resolve(""));
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try { resolve(JSON.parse(data).token || ""); } catch { resolve(""); }
        });
      });
      req.on("error", () => resolve(""));
      req.on("timeout", () => { req.destroy(); resolve(""); });
    });
  } catch {
    return Promise.resolve("");
  }
}

function checkBackend(healthUrl = HEALTH_URL) {
  return new Promise((resolve, reject) => {
    const req = http.get(healthUrl, { timeout: 2000 }, (res) => {
      res.resume();
      res.statusCode < 500 ? resolve() : reject();
    });
    req.on("error", reject);
    req.on("timeout", () => { req.destroy(); reject(); });
  });
}

function waitForBackend(targetWin, healthUrl = HEALTH_URL) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const poll = () => {
      if (targetWin?.isDestroyed()) return reject(new Error("Window closed"));
      const now = Date.now();
      if (now - start > MAX_WAIT_MS) return reject(new Error("Backend timeout"));
      targetWin?.webContents?.send("status", `Waiting for gateway… ${Math.round((now - start) / 1000)}s`);
      checkBackend(healthUrl).then(() => { targetWin?.webContents?.send("status", "Connected ✓"); resolve(); }).catch(() => setTimeout(poll, POLL_INTERVAL_MS));
    };
    poll();
  });
}

// ── Theme-aware modal styles ──

/** Read CSS custom properties from the active KiroClaw dashboard. */
async function getDashboardThemeVars() {
  const win = BaseWindow.getFocusedWindow() || mainWindow;
  if (!win || win.isDestroyed()) return null;
  try {
    return await win.webContents.executeJavaScript(`
      (() => {
        const s = getComputedStyle(document.documentElement);
        return {
          bg: s.getPropertyValue('--bg').trim(),
          card: s.getPropertyValue('--card').trim(),
          text: s.getPropertyValue('--text').trim(),
          muted: s.getPropertyValue('--muted').trim(),
          border: s.getPropertyValue('--border').trim(),
          accent: s.getPropertyValue('--accent').trim(),
          accentHover: s.getPropertyValue('--accent-hover').trim(),
          bgAccent: s.getPropertyValue('--bg-accent').trim(),
        };
      })()
    `);
  } catch {}
  return null;
}

function modalCSSForMode(dark) {
  return `* { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:-apple-system,sans-serif; padding:24px; background:${dark ? "#1e293b" : "#f8fafc"}; color:${dark ? "#e2e8f0" : "#1e293b"}; }
    label { display:block; margin-bottom:8px; font-size:13px; color:${dark ? "#94a3b8" : "#64748b"}; }
    input { width:100%; padding:10px; border-radius:6px; border:1px solid ${dark ? "#475569" : "#cbd5e1"};
      background:${dark ? "#0f172a" : "#ffffff"}; color:${dark ? "#e2e8f0" : "#1e293b"}; font-size:14px; outline:none; margin-bottom:12px; }
    input:focus { border-color:#f97316; }
    .hint { font-size:11px; color:${dark ? "#64748b" : "#94a3b8"}; margin-bottom:12px; }
    .row { display:flex; gap:8px; }
    button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
    .ok { background:#f97316; color:#fff; } .ok:hover { background:#ea580c; }
    .cancel { background:${dark ? "#334155" : "#e2e8f0"}; color:${dark ? "#94a3b8" : "#475569"}; } .cancel:hover { background:${dark ? "#475569" : "#cbd5e1"}; }`;
}

function modalCSSFromVars(v) {
  return `* { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:-apple-system,sans-serif; padding:24px; background:${v.bg}; color:${v.text}; }
    label { display:block; margin-bottom:8px; font-size:13px; color:${v.muted}; }
    input { width:100%; padding:10px; border-radius:6px; border:1px solid ${v.border};
      background:${v.card}; color:${v.text}; font-size:14px; outline:none; margin-bottom:12px; }
    input:focus { border-color:${v.accent}; }
    .hint { font-size:11px; color:${v.muted}; margin-bottom:12px; }
    .row { display:flex; gap:8px; }
    button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
    .ok { background:${v.accent}; color:#fff; } .ok:hover { background:${v.accentHover || v.accent}; }
    .cancel { background:${v.bgAccent || v.card}; color:${v.muted}; } .cancel:hover { background:${v.border}; }`;
}

/** Get modal CSS — reads live theme vars from dashboard, falls back to dark/light mode. */
async function getModalCSS() {
  const vars = await getDashboardThemeVars();
  if (vars && vars.bg) return modalCSSFromVars(vars);
  const dark = nativeTheme.shouldUseDarkColors;
  return modalCSSForMode(dark);
}

// ── Window ──

function syncNativeTheme(view, win) {
  if (win.isDestroyed()) return;
  view.webContents.executeJavaScript(
    `document.documentElement.dataset.mode || ""`
  ).then(mode => {
    if (mode === "dark" || mode === "light") nativeTheme.themeSource = mode;
  }).catch(() => {});
}

function setupWindowContents(win, backendUrl) {
  const port = new URL(backendUrl).port;
  let customName = null;

  // Create a WebContentsView positioned below the tab bar
  const view = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  view.setBackgroundColor("#00000000");
  win.contentView.addChildView(view);

  // Drag region in the tab bar padding area (makes it draggable)
  const dragView = new WebContentsView();
  dragView.setBackgroundColor("#00000000");
  dragView.webContents.loadURL("about:blank");
  dragView.webContents.on("did-finish-load", () => {
    dragView.webContents.insertCSS("html { -webkit-app-region: drag; height: 100%; }");
  });
  win.contentView.addChildView(dragView);

  // Clean up views when window is closed
  win.on("closed", () => {
    view.webContents.close();
    dragView.webContents.close();
  });

  // Position the content view below the tab bar area
  function updateViewBounds() {
    if (win.isDestroyed()) return;
    const { width, height } = win.getContentBounds();
    const offset = win.isFullScreen() ? 0 : TAB_BAR_HEIGHT;
    dragView.setBounds({ x: 0, y: 0, width, height: offset });
    view.setBounds({ x: 0, y: offset, width, height: height - offset });
  }
  updateViewBounds();
  win.on("resize", updateViewBounds);
  win.on("enter-full-screen", updateViewBounds);
  win.on("leave-full-screen", updateViewBounds);
  // The initial updateViewBounds() above runs before win.show() and before the
  // dashboard finishes loading, so getContentBounds() can return a pre-layout
  // size — leaving the WebContentsView mis-sized (content overflows / gets cut
  // off a few seconds in once the window settles to its real size). Recompute
  // on every event that can change the final content size.
  win.on("show", updateViewBounds);
  win.on("restore", updateViewBounds);
  win.on("move", updateViewBounds); // display / scale-factor changes
  view.webContents.on("did-finish-load", () => {
    updateViewBounds();
    // The dashboard loads built-in apps and other content asynchronously after
    // did-finish-load, which can drive a late layout pass; recompute once more
    // shortly after so a content-triggered resize can't leave the view cut off.
    setTimeout(updateViewBounds, 1500);
  });

  // Expose webContents on the window for compatibility
  win.webContents = view.webContents;

  function applyTitle() {
    const suffix = customName || `[:${port}]`;
    win.setTitle(`KiroClaw ${suffix}`);
  }

  win._mcSetCustomName = (name) => { customName = name; applyTitle(); };
  win._mcGetCustomName = () => customName;
  win._mcBackendUrl = backendUrl;
  win._mcView = view;
  attachContextMenu(view.webContents);

  win.on("system-context-menu", (e, point) => {
    e.preventDefault();
    Menu.buildFromTemplate([
      { label: "Rename Tab…", click: () => renameCurrentTab() },
      { type: "separator" },
      { label: "New Connection Tab…", click: () => openNewTab() },
      { label: "Merge All Windows", click: () => mergeAllWindows() },
    ]).popup({ window: win, x: point.x, y: point.y });
  });

  view.webContents.on("did-finish-load", applyTitle);
  view.webContents.on("page-title-updated", (e) => { e.preventDefault(); applyTitle(); });

  view.webContents.on("did-finish-load", () => {
    view.webContents.insertCSS(`
      #electron-drag-bar {
        position: fixed;
        top: 0; left: 0; right: 0;
        height: 52px;
        -webkit-app-region: drag;
        z-index: 99999;
        pointer-events: none;
      }
      a, button, input, select, textarea,
      [role="button"], [tabindex] {
        -webkit-app-region: no-drag;
      }
    `);
    view.webContents.executeJavaScript(`
      if (!document.getElementById('electron-drag-bar')) {
        const bar = document.createElement('div');
        bar.id = 'electron-drag-bar';
        document.body.prepend(bar);
      }
    `);
    // Sync window background to theme color (visible in tab bar padding area)
    view.webContents.executeJavaScript(
      `getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()`
    ).then(bg => { if (bg && !win.isDestroyed()) win.setBackgroundColor(bg); }).catch(() => {});
    // Sync native chrome on first load
    syncNativeTheme(view, win);
  });

  // Sync native tab bar to dashboard dark/light mode on focus (process-global setting)
  win.on("focus", () => syncNativeTheme(view, win));

  view.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const u = new URL(url);
      if (u.origin === new URL(backendUrl).origin) {
        return { action: 'allow' };
      }
      if (u.protocol === 'http:' || u.protocol === 'https:') {
        shell.openExternal(url);
      }
    } catch {}
    return { action: 'deny' };
  });

  view.webContents.session.webRequest.onBeforeSendHeaders((details, callback) => {
    delete details.requestHeaders["Referer"];
    callback({ requestHeaders: details.requestHeaders });
  });
}

function createWindow() {
  mainWindow = new BaseWindow({
    width: 1280,
    height: 860,
    minWidth: 550,
    minHeight: 600,
    tabbingIdentifier: "kiroclaw",
    titleBarStyle: "hidden",
    backgroundColor: "#0f1117",
  });

  setupWindowContents(mainWindow, BACKEND_URL);

  // Auto-refresh token on 403 (gateway secret regenerated after restart)
  const onNavigate = createTokenRetryHandler(() => refreshToken());
  mainWindow.webContents.on("did-navigate", (_e, _url, httpCode) => {
    onNavigate(httpCode).catch((err) => console.error("Token retry failed:", err));
  });

  mainWindow.on("close", (e) => {
    if (!isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  return mainWindow;
}

function createTray() {
  const iconPath = path.join(__dirname, "icon.png");
  const icon = nativeImage.createFromPath(iconPath).resize({ width: 18, height: 18 });
  tray = new Tray(icon);
  tray.setToolTip("KiroClaw");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Show KiroClaw", click: () => mainWindow?.show() },
      { type: "separator" },
      { label: "New Connection Tab…", click: () => openNewTab() },
      { label: "Merge All Windows", click: () => mergeAllWindows() },
      { type: "separator" },
      { label: "Set Remote Host…", click: () => promptRemoteHost() },
      { label: "Refresh Token", click: () => refreshToken() },
      { type: "separator" },
      { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
    ])
  );
  tray.on("click", () => mainWindow?.show());
}

// ── Remote host settings ──

async function promptRemoteHost() {
  const current = store.get("remoteHost") || "";
  const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
  const { response } = await dialog.showMessageBox(parent, {
    type: "question",
    title: "Remote Host",
    message: "Enter your dev desktop hostname for remote token fetch.",
    detail: current
      ? `Current: ${current}\n\nLeave empty and click "Clear" to remove.`
      : "Example: myhost.corp.amazon.com\n\nThis is used to run 'kiroclaw token' via SSH.",
    buttons: ["Configure…", "Clear", "Cancel"],
    defaultId: 0,
  });
  if (response === 2) return; // Cancel
  if (response === 1) {
    store.set("remoteHost", "");
    const clearParent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
    dialog.showMessageBox(clearParent, { message: "Remote host cleared.", type: "info" });
    return;
  }
  // Prompt for the actual hostname using an input dialog
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const win = new BrowserWindow({
    width: 480, height: 220, resizable: false,
    parent: mainWindow, modal: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true, preload: path.join(__dirname, "preload.js") },
  });
  const html = `<!DOCTYPE html><html><head><style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:-apple-system,sans-serif; padding:24px; background:#1e293b; color:#e2e8f0; }
    label { display:block; margin-bottom:8px; font-size:13px; color:#94a3b8; }
    input { width:100%; padding:10px; border-radius:6px; border:1px solid #475569;
      background:#0f172a; color:#e2e8f0; font-size:14px; outline:none; margin-bottom:12px; }
    input:focus { border-color:#f97316; }
    .row { display:flex; gap:8px; }
    button { flex:1; padding:8px; border-radius:6px; border:none; cursor:pointer; font-size:13px; font-weight:600; }
    .save { background:#f97316; color:#fff; } .save:hover { background:#ea580c; }
    .cancel { background:#334155; color:#94a3b8; } .cancel:hover { background:#475569; }
    .hint { font-size:11px; color:#64748b; margin-bottom:12px; }
  </style></head><body>
    <label>Remote dev desktop hostname</label>
    <input id="h" value="${esc(current)}" placeholder="myhost.corp.amazon.com" autofocus>
    <div class="hint">Used to run <code>kiroclaw token</code> via SSH</div>
    <label>kiroclaw binary path</label>
    <input id="b" value="${esc(store.get("kiroclawBinPath"))}" placeholder="$HOME/.local/bin/kiroclaw">
    <div class="row"><button class="save" onclick="save()">Save</button>
    <button class="cancel" onclick="window.close()">Cancel</button></div>
    <script>
      function save() {
        const h = document.getElementById('h').value.trim();
        const b = document.getElementById('b').value.trim();
        document.title = JSON.stringify({host:h, bin:b});
        window.close();
      }
      document.addEventListener('keydown', e => { if(e.key==='Enter') save(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  win.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  // Capture title while window is alive — win.getTitle() throws after "closed" fires.
  let savedTitle = null;
  win.on("page-title-updated", (_e, title) => { savedTitle = title; });
  win.on("closed", () => {
    try {
      if (savedTitle && savedTitle.startsWith("{")) {
        const { host, bin } = JSON.parse(savedTitle);
        const err = validateRemoteSettings(host, bin);
        const parent = mainWindow && !mainWindow.isDestroyed() ? mainWindow : null;
        if (err) {
          dialog.showMessageBox(parent, { type: "error", title: "Invalid Input", message: err });
          return;
        }
        store.set("remoteHost", host);
        if (bin) store.set("kiroclawBinPath", bin);
        console.log(`Remote host set to: ${host}, bin: ${bin}`);
        dialog.showMessageBox(parent, { message: `Remote host set to ${host}`, type: "info" });
      }
    } catch (e) { console.error("Failed to parse remote host settings:", e.message); }
  });
}

async function refreshToken() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  let token = await fetchLocalToken();
  if (!token) token = await fetchRemoteToken();
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (token) {
    mainWindow.webContents.loadURL(`${BACKEND_URL}?token=${token}`);
  } else {
    dialog.showMessageBox(mainWindow, {
      type: "warning",
      title: "Token Refresh",
      message: "Could not fetch a fresh token.",
      detail: store.get("remoteHost")
        ? "SSH to remote host failed. Check your connection."
        : "No remote host configured. Use 'Set Remote Host…' in the tray menu.",
    });
  }
}

// ── Loading screen ──

async function showLoadingThenConnect(win, backendUrl = BACKEND_URL) {
  const healthUrl = `${backendUrl}/api/status`;
  const wc = win.webContents;
  wc.loadFile(path.join(__dirname, "loading.html"));
  win.show();

  try {
    await waitForBackend(win, healthUrl);
    if (win.isDestroyed()) return;
    let token = await fetchLocalToken(backendUrl);
    if (!token && backendUrl === BACKEND_URL) token = await fetchRemoteToken();
    if (win.isDestroyed()) return;

    if (token) {
      wc.loadURL(`${backendUrl}?token=${token}`);
    } else {
      // Fallback — check if gateway allows unauthenticated access
      const status = await new Promise((resolve) => {
        http.get(backendUrl, (res) => {
          res.resume();
          resolve(res.statusCode);
        }).on("error", () => resolve(0));
      });
      if (win.isDestroyed()) return;
      if (status === 403) {
        wc.loadFile(path.join(__dirname, "token-prompt.html"), { query: { port: new URL(backendUrl).port } });
      } else {
        wc.loadURL(backendUrl);
      }
    }
  } catch {
    if (win.isDestroyed()) return;
    const { response } = await dialog.showMessageBox(win, {
      type: "error",
      title: "KiroClaw",
      message: "Could not connect to the KiroClaw backend.",
      detail: `Make sure 'kiroclaw gateway' is running, or check kiroclaw doctor.`,
      buttons: ["Retry", "Quit"],
    });
    if (response === 0) return showLoadingThenConnect(win, backendUrl);
    if (win === mainWindow) {
      isQuitting = true;
      app.quit();
    } else {
      win.destroy();
    }
  }
}

// ── New Connection Tab ──

async function openNewTab() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.show();

  const css = await getModalCSS();
  const promptWin = new BrowserWindow({
    width: 400, height: 180, resizable: false, useContentSize: true,
    parent: mainWindow, modal: true, backgroundColor: "#00000000",
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const html = `<!DOCTYPE html><html><head><style>
    ${css}
  </style></head><body>
    <label>Gateway port</label>
    <input id="p" type="number" value="7778" min="1" max="65535" autofocus>
    <div class="hint">Connect to a KiroClaw gateway running on another port</div>
    <div class="row"><button class="ok" onclick="go()">Connect</button>
    <button class="cancel" onclick="window.close()">Cancel</button></div>
    <script>
      function go() { document.title = document.getElementById('p').value.trim(); window.close(); }
      document.addEventListener('keydown', e => { if(e.key==='Enter') go(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  promptWin.setMenu(null);

  let savedTitle = null;
  promptWin.on("page-title-updated", (_e, title) => { savedTitle = title; });
  promptWin.on("closed", async () => {
    if (!savedTitle) return;
    const port = parseInt(savedTitle, 10);
    if (isNaN(port) || port < 1 || port > 65535) return;
    if (!mainWindow || mainWindow.isDestroyed()) return;

    const backendUrl = `http://localhost:${port}`;
    const tabWin = new BaseWindow({
      width: 1280,
      height: 860,
      minWidth: 550,
      minHeight: 600,
      tabbingIdentifier: "kiroclaw",
      titleBarStyle: "hidden",
      backgroundColor: "#0f1117",
    });

    setupWindowContents(tabWin, backendUrl);

    const onNavigate = createTokenRetryHandler(async () => {
      const token = await fetchLocalToken(backendUrl);
      if (token && !tabWin.isDestroyed()) {
        tabWin.webContents.loadURL(`${backendUrl}?token=${token}`);
      }
    });
    tabWin.webContents.on("did-navigate", (_e, _url, httpCode) => {
      onNavigate(httpCode).catch((err) => console.error("Token retry failed:", err));
    });

    mainWindow.addTabbedWindow(tabWin);
    await showLoadingThenConnect(tabWin, backendUrl);
  });
}

// ── Rename Tab ──

function renameCurrentTab() {
  const focused = BaseWindow.getFocusedWindow();
  if (!focused || !focused._mcSetCustomName) return;

  const currentTitle = focused.getTitle();
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  getDashboardThemeVars().then((vars) => {
  const css = vars && vars.bg ? modalCSSFromVars(vars) : modalCSSForMode(nativeTheme.shouldUseDarkColors);
  const promptWin = new BrowserWindow({
    width: 400, height: 180, resizable: false, useContentSize: true,
    parent: focused, modal: true, backgroundColor: "#00000000",
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const html = `<!DOCTYPE html><html><head><style>
    ${css}
  </style></head><body>
    <label>Tab name</label>
    <input id="n" value="${esc(currentTitle.replace(/^KiroClaw /g, ''))}" autofocus>
    <div class="row"><button class="ok" onclick="go()">Rename</button>
    <button class="cancel" onclick="window.close()">Cancel</button></div>
    <script>
      function go() { document.title = document.getElementById('n').value.trim(); window.close(); }
      document.addEventListener('keydown', e => { if(e.key==='Enter') go(); if(e.key==='Escape') window.close(); });
    </script>
  </body></html>`;
  promptWin.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
  promptWin.setMenu(null);

  let savedTitle = null;
  promptWin.on("page-title-updated", (_e, title) => { savedTitle = title; });
  promptWin.on("closed", () => {
    if (savedTitle && focused && !focused.isDestroyed()) {
      focused._mcSetCustomName(savedTitle);
    }
  });
  }); // end getDashboardThemeVars().then()
}

// ── Merge Windows ──

function mergeAllWindows() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.show();

  const others = BaseWindow.getAllWindows().filter(
    (w) => w !== mainWindow && !w.isDestroyed() && w._mcSetCustomName
  );
  for (const win of others) {
    mainWindow.addTabbedWindow(win);
  }
  // Force tab bar redraw after merge
  setTimeout(() => {
    if (!mainWindow.isDestroyed()) {
      mainWindow.setHasShadow(false);
      mainWindow.setHasShadow(true);
    }
  }, 50);
}

// ── App lifecycle ──

// Guide the user to grant macOS Screen Recording permission when it has been
// explicitly denied — the snip tool cannot capture any frame without it. Opens
// the exact Privacy pane. Note: the granted entity must be the packaged
// KiroClaw.app, not the terminal that launched a dev build.
function showScreenPermissionDialog() {
  const pane = "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture";
  dialog
    .showMessageBox({
      type: "info",
      title: "Screen Recording permission needed",
      message: "Allow KiroClaw to capture the screen",
      detail:
        "The screen-snip tool needs macOS Screen Recording permission. Open System Settings › Privacy & Security › Screen Recording, enable KiroClaw, then try the snip again.",
      buttons: ["Open System Settings", "Cancel"],
      defaultId: 0,
      cancelId: 1,
    })
    .then(({ response }) => {
      if (response === 0) shell.openExternal(pane);
    })
    .catch(() => {});
}

app.whenReady().then(async () => {
  // App menu with Rename Tab shortcut
  const appMenu = Menu.buildFromTemplate([
    { role: "appMenu" },
    { role: "editMenu" },
    {
      label: "Tab",
      submenu: [
        { label: "New Connection Tab…", accelerator: "CmdOrCtrl+T", click: () => openNewTab() },
        { label: "Rename Tab…", accelerator: "CmdOrCtrl+Shift+R", click: () => renameCurrentTab() },
        { type: "separator" },
        { label: "Merge All Windows", click: () => mergeAllWindows() },
      ],
    },
    { role: "windowMenu" },
  ]);
  Menu.setApplicationMenu(appMenu);

  // Enable the chat input's screen-snip tool inside the Electron shell.
  // Without a display-media request handler, Electron (>= 20) rejects the
  // renderer's navigator.mediaDevices.getDisplayMedia(), so the snip button
  // silently no-ops in the packaged app (it works in a plain browser because
  // Chromium shows the OS picker natively). useSystemPicker uses macOS's native
  // screen picker when available; the desktopCapturer-backed handler is the
  // fallback for older macOS / other platforms.
  session.defaultSession.setDisplayMediaRequestHandler(
    createDisplayMediaHandler({
      getSources: () => desktopCapturer.getSources({ types: ["screen", "window"] }),
      getScreenAccessStatus: () =>
        process.platform === "darwin"
          ? systemPreferences.getMediaAccessStatus("screen")
          : "granted",
      onPermissionNeeded: (reason) => {
        if (reason === "denied") showScreenPermissionDialog();
      },
    }),
    { useSystemPicker: true },
  );

  createTray();
  const win = createWindow();

  await startGateway();
  await showLoadingThenConnect(win);

  app.on("activate", () => {
    if (!mainWindow?.isVisible()) mainWindow?.show();
  });

  app.on("new-window-for-tab", () => {
    openNewTab();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
  stopGateway();
});

app.on("window-all-closed", () => {
  // macOS: keep running in tray
  if (process.platform !== "darwin") app.quit();
});
