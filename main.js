const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const fs   = require('fs');
const { spawn } = require('child_process');

let win;
let pyProcess;

// ── Icon: try multiple formats ──────────────────────────────────────
function getIconPath() {
  const names = ['icon.png']
  const dirs  = [__dirname, path.join(__dirname, 'assets'), path.join(__dirname, 'resources')];
  for (const dir of dirs) {
    for (const name of names) {
      const p = path.join(dir, name);
      if (fs.existsSync(p)) {
        console.log('[Main] Found icon:', p);
        return p;
      }
    }
  }
  console.log('[Main] No icon found. Place icon.png in app root directory.');
  return undefined;
}

// ── Window ──────────────────────────────────────────────────────────
function createWindow() {
  const iconPath = getIconPath();
  console.log('[Main] Icon path:', iconPath || '(none found — using default)');

  win = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1200,
    minHeight: 800,
    frame: false,
    backgroundColor: '#080e0a',
    ...(iconPath ? { icon: iconPath } : {}),
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });

  win.loadFile('index.html');
  startPython();

  // Send user-provided build strings to renderer
  setTimeout(() => {
    try {
      let builds = {};
      if (fs.existsSync(BUILDS_FILE)) {
        builds = JSON.parse(fs.readFileSync(BUILDS_FILE, 'utf-8'));
      }
      win?.webContents.send('from-python', JSON.stringify({
        status: 'talent_builds_loaded', builds
      }));
    } catch (e) {
      console.error('[Main] Error reading talent_builds.json:', e.message);
    }
  }, 1500);
}

// ── Python backend ──────────────────────────────────────────────────
function startPython() {
  const script = path.join(__dirname, 'engine.py');
  pyProcess = spawn('python', [script], {
    cwd: __dirname,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let buffer = '';
  pyProcess.stdout.on('data', (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split('\n');
    buffer = lines.pop();
    lines.forEach(line => {
      line = line.trim();
      if (line) win?.webContents.send('from-python', line);
    });
  });

  pyProcess.stderr.on('data', (d) => console.error('[Python]', d.toString().trim()));
  pyProcess.on('close', (code) => console.log('[Python] Exited with code', code));

  // Wait for ready signal, then request characters
  let ready = false;
  const handler = (_, raw) => {
    if (ready) return;
    try {
      const data = JSON.parse(raw);
      if (data.status === 'ready') {
        ready = true;
        pyProcess.stdin.write('GET_CHARACTERS\n');
      }
    } catch {}
  };
  ipcMain.on('from-python-internal', handler);

  pyProcess.stdout.on('data', (chunk) => {
    chunk.toString().split('\n').forEach(line => {
      line = line.trim();
      if (line) ipcMain.emit('from-python-internal', null, line);
    });
  });
}

// ── IPC ─────────────────────────────────────────────────────────────
const BUILDS_FILE = path.join(__dirname, 'talent_builds.json');

ipcMain.on('to-python', (_, cmd) => {
  // Intercept build-string saves (handled by main, not Python)
  if (cmd.startsWith('SAVE_BUILD_STRING:')) {
    // Format: SAVE_BUILD_STRING:class:spec:type:string
    const parts = cmd.split(':');
    if (parts.length >= 5) {
      const [, classSlug, specSlug, buildType, ...rest] = parts;
      const buildString = rest.join(':'); // in case string contains ':'
      try {
        let builds = {};
        if (fs.existsSync(BUILDS_FILE)) {
          builds = JSON.parse(fs.readFileSync(BUILDS_FILE, 'utf-8'));
        }
        if (!builds[classSlug]) builds[classSlug] = {};
        if (!builds[classSlug][specSlug]) builds[classSlug][specSlug] = {};
        builds[classSlug][specSlug][buildType] = buildString;
        fs.writeFileSync(BUILDS_FILE, JSON.stringify(builds, null, 2));
        console.log(`[Main] Saved build string: ${classSlug}/${specSlug}/${buildType}`);
        win?.webContents.send('from-python', JSON.stringify({
          status: 'build_string_saved',
          class_slug: classSlug, spec_slug: specSlug, build_type: buildType
        }));
      } catch (e) {
        console.error('[Main] Error saving build string:', e.message);
      }
    }
    return;
  }
  pyProcess?.stdin.write(cmd + '\n');
});

ipcMain.on('window-close',    () => win?.close());
ipcMain.on('window-minimize', () => win?.minimize());
ipcMain.on('window-maximize', () => win?.isMaximized() ? win.unmaximize() : win.maximize());
ipcMain.on('open-external',   (_, url) => shell.openExternal(url));

// ── App lifecycle ───────────────────────────────────────────────────
app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  pyProcess?.stdin.write('EXIT\n');
  if (process.platform !== 'darwin') app.quit();
});