const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron');
const path  = require('path');
const fs    = require('fs');
const https = require('https');
const { spawn } = require('child_process');

let win;
let pyProcess;

// ── Packaging helpers ────────────────────────────────────────────────
const isPackaged = app.isPackaged;

function getDataDir() {
  return isPackaged ? app.getPath('userData') : __dirname;
}

function getBuildsFile() {
  return isPackaged
    ? path.join(getDataDir(), 'talent_builds.json')
    : path.join(__dirname, 'assets', 'talent_builds.json');
}

// ── Icon: try multiple formats ──────────────────────────────────────
function getIconPath() {
  const names = ['icon.png', 'icon.ico', 'icon.icns', 'logo.png', 'logo.ico',
                 'innkeeper.png', 'innkeeper.ico', 'innkeeper.icns',
                 'icon_256.png', 'icon_48.png', 'app-icon.png'];
  const dirs  = [path.join(__dirname, 'assets'), __dirname, path.join(__dirname, 'resources')];
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

// ── Data seeding (first-run: copy bundled files to userData) ─────────
function seedDataFiles() {
  if (!isPackaged) return;

  const dataDir = getDataDir();
  fs.mkdirSync(dataDir, { recursive: true });

  // Copy talent_builds.json from resources to userData on first run
  const destBuilds = path.join(dataDir, 'talent_builds.json');
  if (!fs.existsSync(destBuilds)) {
    const srcBuilds = path.join(process.resourcesPath, 'talent_builds.json');
    if (fs.existsSync(srcBuilds)) {
      fs.copyFileSync(srcBuilds, destBuilds);
      console.log('[Main] Seeded talent_builds.json →', destBuilds);
    }
  }
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

  // Disable menu and DevTools in packaged builds
  if (isPackaged) {
    Menu.setApplicationMenu(null);
    win.webContents.on('devtools-opened', () => win.webContents.closeDevTools());
  }

  win.loadFile(path.join(__dirname, 'frontend', 'index.html'));
  seedDataFiles();
  startPython();

  // Wait for renderer to be ready before sending data
  win.webContents.once('did-finish-load', () => {
    // Step 1: Send build strings FIRST (must be in TALENT_BUILDS before trees render)
    const buildsFile = getBuildsFile();
    try {
      let builds = {};
      if (fs.existsSync(buildsFile)) {
        const raw = JSON.parse(fs.readFileSync(buildsFile, 'utf-8'));
        // Strip keys starting with _ (like _README, _example_format)
        for (const key of Object.keys(raw)) {
          if (!key.startsWith('_')) builds[key] = raw[key];
        }
        console.log(`[Main] Loaded talent_builds.json: classes = [${Object.keys(builds).join(', ')}]`);
      }
      win?.webContents.send('from-python', JSON.stringify({
        status: 'talent_builds_loaded', builds
      }));
    } catch (e) {
      console.error('[Main] Error reading talent_builds.json:', e.message);
    }

    // Step 2: Pre-load disk-cached talent trees (TALENT_BUILDS is already set above)
    try {
      const cacheDir = path.join(getDataDir(), 'talent_tree_cache');
      if (fs.existsSync(cacheDir)) {
        const files = fs.readdirSync(cacheDir).filter(f => f.endsWith('.json'));
        for (const file of files) {
          try {
            const data = JSON.parse(fs.readFileSync(path.join(cacheDir, file), 'utf-8'));
            const base = file.replace('.json', '');
            const sepIdx = base.lastIndexOf('_');
            if (sepIdx > 0 && data.class_nodes && data.spec_nodes) {
              const class_slug = base.substring(0, sepIdx);
              const spec_slug = base.substring(sepIdx + 1);
              win?.webContents.send('from-python', JSON.stringify({
                status: 'talent_tree', class_slug, spec_slug, tree: data
              }));
              console.log(`[Main] Pre-loaded cached tree: ${class_slug}/${spec_slug} (${data.class_nodes.length} class + ${data.spec_nodes.length} spec nodes)`);
            }
          } catch (fe) {
            console.error(`[Main] Error reading cache file ${file}:`, fe.message);
          }
        }
      }
    } catch (e) {
      console.error('[Main] Error scanning talent_tree_cache:', e.message);
    }

    // Step 3: Check for app updates (silent)
    checkForUpdates();
  });
}

// ── Python backend ──────────────────────────────────────────────────
function startPython() {
  const dataDir = getDataDir();

  if (isPackaged) {
    // Packaged: run the bundled engine executable
    const ext = process.platform === 'win32' ? '.exe' : '';
    const enginePath = path.join(process.resourcesPath, 'engine' + ext);

    // Ensure engine is executable on Linux
    if (process.platform !== 'win32') {
      try { fs.chmodSync(enginePath, 0o755); } catch (e) {}
    }

    // Ensure data dir exists
    fs.mkdirSync(dataDir, { recursive: true });

    pyProcess = spawn(enginePath, ['--datadir', dataDir], {
      cwd: dataDir,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    console.log('[Main] Spawned packaged engine:', enginePath, '--datadir', dataDir);
  } else {
    // Dev: run python engine.py as before
    const script = path.join(__dirname, 'backend', 'engine.py');
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
    pyProcess = spawn(pythonCmd, [script, '--datadir', __dirname], {
      cwd: __dirname,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
  }

  let buffer = '';
  let ready = false;
  pyProcess.stdout.on('data', (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split('\n');
    buffer = lines.pop();
    lines.forEach(line => {
      line = line.trim();
      if (!line) return;
      win?.webContents.send('from-python', line);
      if (!ready) {
        try {
          const data = JSON.parse(line);
          if (data.status === 'ready') {
            ready = true;
            pyProcess.stdin.write('GET_CHARACTERS\n');
          }
        } catch {}
      }
    });
  });

  pyProcess.stderr.on('data', (d) => console.error('[Python]', d.toString().trim()));
  pyProcess.on('close', (code) => console.log('[Python] Exited with code', code));
}

// ── IPC ─────────────────────────────────────────────────────────────

ipcMain.on('to-python', (_, cmd) => {
  // Intercept build-string saves (handled by main, not Python)
  if (cmd.startsWith('SAVE_BUILD_STRING:')) {
    // Format: SAVE_BUILD_STRING:class:spec:type:string
    const parts = cmd.split(':');
    if (parts.length >= 5) {
      const [, classSlug, specSlug, buildType, ...rest] = parts;
      const buildString = rest.join(':'); // in case string contains ':'
      const buildsFile = getBuildsFile();
      try {
        let builds = {};
        if (fs.existsSync(buildsFile)) {
          builds = JSON.parse(fs.readFileSync(buildsFile, 'utf-8'));
        }
        if (!builds[classSlug]) builds[classSlug] = {};
        if (!builds[classSlug][specSlug]) builds[classSlug][specSlug] = {};
        builds[classSlug][specSlug][buildType] = buildString;
        fs.writeFileSync(buildsFile, JSON.stringify(builds, null, 2));
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
ipcMain.on('open-external',   (_, url) => {
  try { if (/^https?:\/\//i.test(url)) shell.openExternal(url); }
  catch (e) { console.error('[Main] open-external error:', e.message); }
});

// ── Update check ────────────────────────────────────────────────────
function checkForUpdates() {
  const options = {
    hostname: 'api.github.com',
    path: '/repos/eightmouse/Innkeper/releases/latest',
    headers: { 'User-Agent': 'Innkeeper-App' },
  };
  https.get(options, (res) => {
    let body = '';
    res.on('data', (chunk) => { body += chunk; });
    res.on('end', () => {
      try {
        const release = JSON.parse(body);
        const latestVersion = (release.tag_name || '').replace(/^v/, '');
        const currentVersion = app.getVersion();
        const updateAvailable = latestVersion && latestVersion !== currentVersion;
        win?.webContents.send('from-python', JSON.stringify({
          status: 'update_check',
          updateAvailable,
          latestVersion,
        }));
      } catch (_) { /* silent */ }
    });
  }).on('error', () => { /* silent */ });
}

// ── App lifecycle ───────────────────────────────────────────────────
app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  pyProcess?.stdin.write('EXIT\n');
  if (process.platform !== 'darwin') app.quit();
});
