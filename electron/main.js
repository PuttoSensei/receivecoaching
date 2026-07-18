// Electron main process.
// Responsibilities:
//   1. Spawn the Python backend (server.py) at startup on a random free port
//   2. Wait for it to become healthy
//   3. Open the main window with the backend URL baked into the page
//   4. Kill the backend cleanly on quit

const { app, BrowserWindow, ipcMain, dialog, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const net = require('net');
const crypto = require('crypto');
const { spawn } = require('child_process');

// ---------- Config ----------
//
// Path resolution differs between dev and packaged modes:
//
//  DEV (electron .):
//    __dirname = <repo>/electron/
//    REPO_ROOT = <repo>/
//    Python files live in REPO_ROOT.
//
//  PACKAGED (portable .exe):
//    __dirname points inside the asar archive — can't spawn from there.
//    Python files are shipped in extraResources under `resources/app/...`.
//    REPO_ROOT = process.resourcesPath + '/app'
//
//    User data (data/, logs/, and the coaches/*/.embeddings.json caches)
//    is read-only inside resources/ if we leave it there, but on the
//    first write attempt the Python backend would fail. We redirect those
//    writable paths to userData/ (Windows: %APPDATA%/Receive Coaching/)
//    via environment variables the backend is taught to honour.

const PYTHON_CMD = process.platform === 'win32' ? 'python' : 'python3';

function resolveRepoRoot() {
  if (app.isPackaged) {
    // extraResources go to `<app>/resources/app/...` — this is where the
    // Python scripts, config, and coaches live in a packaged build.
    return path.join(process.resourcesPath, 'app');
  }
  return path.resolve(__dirname, '..');
}

function resolveUiRoot() {
  // The UI (electron/, ui/) ships INSIDE app.asar via the electron-builder
  // `files` config, so it must be loaded through app.getAppPath() (which
  // transparently resolves paths inside the asar archive). Loading it via
  // REPO_ROOT (resources/app/) would 404 because ui/ is not extracted there.
  if (app.isPackaged) {
    return app.getAppPath();
  }
  return path.resolve(__dirname, '..');
}

function resolveWritableRoot() {
  // Where data/, logs/ should live so the user keeps them across builds.
  if (app.isPackaged) {
    return app.getPath('userData');
  }
  return path.resolve(__dirname, '..');
}

const REPO_ROOT = resolveRepoRoot();
const UI_ROOT = resolveUiRoot();
const WRITABLE_ROOT = resolveWritableRoot();
const SERVER_SCRIPT = path.join(REPO_ROOT, 'server.py');

let backendProc = null;
let backendPort = null;
let backendToken = null;
let mainWindow = null;
let quitting = false;   // suppress the backend-died banner during normal quit

// ---------- Helpers ----------

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForHealth(port, token, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const req = http.get({
        host: '127.0.0.1',
        port,
        path: '/api/health',
        timeout: 2000,
        headers: { Authorization: `Bearer ${token}` },
      }, (res) => {
        if (res.statusCode === 200) {
          res.resume();
          resolve();
        } else {
          res.resume();
          retry();
        }
      });
      req.on('error', retry);
      req.on('timeout', () => { req.destroy(); retry(); });
    };
    const retry = () => {
      if (Date.now() > deadline) {
        reject(new Error('Backend did not respond within 30s'));
        return;
      }
      setTimeout(tryOnce, 400);
    };
    tryOnce();
  });
}

function resolveBackendCommand() {
  // Prefer the PyInstaller-bundled backend (shipped in resources/app/pyserver/)
  // so the packaged app doesn't require Python on the host. Fall back to
  // system Python for dev mode or a from-source install.
  const bundled = path.join(REPO_ROOT, 'pyserver', 'server.exe');
  if (app.isPackaged && fs.existsSync(bundled)) {
    return { cmd: bundled, baseArgs: [] };
  }
  return { cmd: PYTHON_CMD, baseArgs: [SERVER_SCRIPT] };
}

async function startBackend() {
  backendPort = await findFreePort();
  // Random per-session shared secret. The renderer receives it via the URL
  // hash and passes it as a Bearer token; the backend requires it. This is
  // what stops any other page on the machine (e.g. a browser tab the user
  // has open) from calling into 127.0.0.1:<port> and reading their memory.
  backendToken = crypto.randomBytes(32).toString('hex');
  const env = { ...process.env };
  // Tell the Python backend where to read static resources (coaches, configs)
  // and where to write user data (memory, logs, embedding caches). In dev these
  // are the same folder; in a packaged build they diverge.
  env.RECEIVE_COACH_REPO_ROOT = REPO_ROOT;
  env.RECEIVE_COACH_DATA_ROOT = WRITABLE_ROOT;
  env.RECEIVE_COACH_AUTH_TOKEN = backendToken;
  const { cmd, baseArgs } = resolveBackendCommand();
  backendProc = spawn(
    cmd,
    [...baseArgs, '--host', '127.0.0.1', '--port', String(backendPort)],
    { cwd: REPO_ROOT, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }
  );
  backendProc.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  backendProc.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  backendProc.on('exit', (code) => {
    console.log(`[backend] exited with code ${code}`);
    backendProc = null;
    // Tell the renderer so it can show a banner instead of silently failing
    // every request from here on. (During normal quit the window is already
    // gone and this is a no-op.)
    if (!quitting && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('backend-exited', code);
    }
  });

  try {
    await waitForHealth(backendPort, backendToken);
  } catch (err) {
    dialog.showErrorBox(
      'Backend failed to start',
      `Couldn't reach the local backend on 127.0.0.1:${backendPort}.\n\n${err.message}\n\n` +
      `Common causes:\n` +
      ` - Antivirus quarantined the bundled server (resources/app/pyserver/)\n` +
      ` - Running from source without Python on PATH\n` +
      ` - From source: pip install fastapi uvicorn\n` +
      ` - receive_coach.py import error (see console)`
    );
    app.quit();
    throw err;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 600,
    backgroundColor: '#0f1115',
    title: 'Receive Coaching',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // Load the UI and pass the backend port + auth token as URL hash params.
  const uiPath = path.join(UI_ROOT, 'ui', 'index.html');
  mainWindow.loadFile(uiPath, { hash: `port=${backendPort}&token=${backendToken}` });

  // Open DevTools if --dev was passed on command line
  if (process.argv.includes('--dev')) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  // Open external http(s) links in the OS browser; never open child windows
  // in-app (file://, about:, etc. would otherwise pop a window with local
  // filesystem access).
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://') || url.startsWith('https://')) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  // The shell is a single fixed page — block all navigation away from it so
  // an injected location change can't replace the UI with a remote or
  // file:// page.
  mainWindow.webContents.on('will-navigate', (e) => e.preventDefault());

  // Voice input uses getUserMedia; grant microphone only, deny everything else.
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, permission, callback) => {
    callback(permission === 'media');
  });
}

// ---------- IPC ----------

ipcMain.handle('get-backend-port', () => backendPort);

// Paths the user explicitly selected in the native picker. read-file refuses
// anything else — without this, any script that gains a foothold in the
// renderer could exfiltrate arbitrary local files (SSH keys, browser data).
const approvedReadPaths = new Set();
const MAX_READ_BYTES = 10 * 1024 * 1024;

ipcMain.handle('pick-file-and-upload', async (_evt, { coachName }) => {
  // Used by "Add source" button. Returns file path(s) selected.
  const result = await dialog.showOpenDialog(mainWindow, {
    title: `Add a source file to ${String(coachName).slice(0, 80)}`,
    properties: ['openFile', 'multiSelections'],
    filters: [{ name: 'Text / Markdown / PDF', extensions: ['txt', 'md', 'pdf'] }],
  });
  if (result.canceled) return [];
  for (const p of result.filePaths) approvedReadPaths.add(p);
  return result.filePaths.map((p) => ({ path: p, name: path.basename(p) }));
});

ipcMain.handle('read-file', async (_evt, filePath) => {
  // Read a user-picked file and return as base64 (used by upload in renderer).
  if (typeof filePath !== 'string' || !approvedReadPaths.has(filePath)) {
    throw new Error('read-file: path was not selected via the file picker');
  }
  const stat = fs.statSync(filePath);
  if (stat.size > MAX_READ_BYTES) {
    throw new Error(`read-file: file exceeds ${MAX_READ_BYTES / (1024 * 1024)} MB limit`);
  }
  const data = fs.readFileSync(filePath);
  return { name: path.basename(filePath), data: data.toString('base64') };
});

// ---------- App lifecycle ----------

app.whenReady().then(async () => {
  try {
    await startBackend();
    createWindow();
  } catch (err) {
    console.error(err);
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

function shutdownBackend() {
  quitting = true;
  if (backendProc && !backendProc.killed) {
    try { backendProc.kill('SIGTERM'); } catch (_) {}
    // Force kill after grace period
    setTimeout(() => { try { backendProc && backendProc.kill('SIGKILL'); } catch (_) {} }, 2000);
  }
}

app.on('window-all-closed', () => {
  shutdownBackend();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', shutdownBackend);
app.on('will-quit', shutdownBackend);
