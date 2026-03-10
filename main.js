const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron');
const path  = require('path');
const fs    = require('fs');
const https = require('https');
const { spawn } = require('child_process');

let win;
let pyProcess;
let housingCacheStale = true;
let _buildsCache = null; // in-memory builds, loaded once at startup

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
async function getIconPath() {
  const fsp = fs.promises;
  const names = ['icon.png', 'icon.ico', 'icon.icns', 'logo.png', 'logo.ico',
                 'innkeeper.png', 'innkeeper.ico', 'innkeeper.icns',
                 'icon_256.png', 'icon_48.png', 'app-icon.png'];
  const dirs  = [path.join(__dirname, 'assets'), __dirname, path.join(__dirname, 'resources')];
  for (const dir of dirs) {
    for (const name of names) {
      const p = path.join(dir, name);
      try {
        await fsp.access(p);
        console.log('[Main] Found icon:', p);
        return p;
      } catch {}
    }
  }
  console.log('[Main] No icon found. Place icon.png in app root directory.');
  return undefined;
}

// ── Data seeding (first-run: copy bundled files to userData) ─────────
async function seedDataFiles() {
  if (!isPackaged) return;

  const fsp = fs.promises;
  const dataDir = getDataDir();
  await fsp.mkdir(dataDir, { recursive: true });

  // Copy talent_builds.json from resources to userData on first run
  const destBuilds = path.join(dataDir, 'talent_builds.json');
  try {
    await fsp.access(destBuilds);
  } catch {
    const srcBuilds = path.join(process.resourcesPath, 'talent_builds.json');
    try {
      await fsp.access(srcBuilds);
      await fsp.copyFile(srcBuilds, destBuilds);
      console.log('[Main] Seeded talent_builds.json →', destBuilds);
    } catch {}
  }
}

// ── Window ──────────────────────────────────────────────────────────
async function createWindow() {
  const iconPath = await getIconPath();
  console.log('[Main] Icon path:', iconPath || '(none found — using default)');

  win = new BrowserWindow({
    title: 'Innkeeper',
    width: 1600,
    height: 1000,
    minWidth: 1200,
    minHeight: 800,
    frame: false,
    backgroundColor: '#080e0a',
    ...(iconPath ? { icon: iconPath } : {}),
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'frontend', 'preload.js'),
    },
  });

  // Disable menu and DevTools in packaged builds
  if (isPackaged) {
    Menu.setApplicationMenu(null);
    win.webContents.on('devtools-opened', () => win.webContents.closeDevTools());
  }

  win.loadFile(path.join(__dirname, 'frontend', 'index.html'));

  // Register listener SYNCHRONOUSLY before any awaits to avoid race with loadFile
  win.webContents.once('did-finish-load', async () => {
    const fsp = fs.promises;

    // Step 1: Send build strings FIRST (must be in TALENT_BUILDS before trees render)
    {
      _buildsCache = {};
      try {
        const buildsFile = getBuildsFile();
        const raw = JSON.parse(await fsp.readFile(buildsFile, 'utf-8'));
        for (const key of Object.keys(raw)) {
          if (!key.startsWith('_')) _buildsCache[key] = raw[key];
        }
        console.log(`[Main] Loaded talent_builds.json: classes = [${Object.keys(_buildsCache).join(', ')}]`);
      } catch (e) {
        console.error('[Main] Error reading talent_builds.json:', e.message);
      }
      win?.webContents.send('from-python', JSON.stringify({
        status: 'talent_builds_loaded', builds: _buildsCache
      }));
    }

    // Step 2: Pre-load disk-cached talent trees (TALENT_BUILDS is already set above)
    try {
      const cacheDir = path.join(getDataDir(), 'talent_tree_cache');
      const files = (await fsp.readdir(cacheDir)).filter(f => f.endsWith('.json'));
      for (const file of files) {
        try {
          const data = JSON.parse(await fsp.readFile(path.join(cacheDir, file), 'utf-8'));
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
    } catch (e) {
      console.error('[Main] Error scanning talent_tree_cache:', e.message);
    }

    // Step 3: Pre-load housing decorations catalog
    try {
      const housingFile = path.join(__dirname, 'assets', 'housing_decorations.json');
      const catalog = JSON.parse(await fsp.readFile(housingFile, 'utf-8'));
      win?.webContents.send('from-python', JSON.stringify({
        status: 'housing_catalog_loaded', catalog
      }));
      console.log(`[Main] Loaded housing_decorations.json: ${(catalog.items || []).length} items`);
    } catch (e) {
      console.error('[Main] Error reading housing_decorations.json:', e.message);
    }

    // Step 4: Pre-load housing decor catalog (cache → bundled fallback)
    try {
      let apiLoaded = false;
      const apiCacheFile = path.join(getDataDir(), 'housing_decor_cache', 'decor_catalog.json');
      try {
        const raw = JSON.parse(await fsp.readFile(apiCacheFile, 'utf-8'));
        const hasIcons = (raw.items || []).slice(0, 50).some(i => i.icon_url);
        if (raw.fetched_at && hasIcons) {
          const ageMs = Date.now() - new Date(raw.fetched_at).getTime();
          if (ageMs < 7 * 24 * 3600 * 1000) {
            win?.webContents.send('from-python', JSON.stringify({
              status: 'housing_api_catalog', catalog: raw
            }));
            housingCacheStale = false;
            apiLoaded = true;
            console.log(`[Main] Pre-loaded cached housing catalog: ${(raw.items || []).length} items (age=${(ageMs / 3600000).toFixed(1)}h)`);
          }
        }
      } catch {}
      if (!apiLoaded) {
        const bundledFile = path.join(__dirname, 'assets', 'housing_decor_enriched.json');
        const raw = JSON.parse(await fsp.readFile(bundledFile, 'utf-8'));
        win?.webContents.send('from-python', JSON.stringify({
          status: 'housing_api_catalog', catalog: raw
        }));
        console.log(`[Main] Pre-loaded bundled housing catalog: ${(raw.items || []).length} items`);
      }
    } catch (e) {
      console.error('[Main] Error reading housing catalog:', e.message);
    }

    // Step 5: Pre-load housing source data (Wowhead acquisition info)
    try {
      const sourcesFile = path.join(__dirname, 'assets', 'housing_sources.json');
      const sources = JSON.parse(await fsp.readFile(sourcesFile, 'utf-8'));
      const count = Object.keys(sources).filter(k => sources[k]).length;
      win?.webContents.send('from-python', JSON.stringify({
        status: 'housing_sources_loaded', sources
      }));
      console.log(`[Main] Loaded housing_sources.json: ${count} items with source info`);
    } catch (e) {
      console.error('[Main] Error reading housing_sources.json:', e.message);
    }

    // Step 6: Check for app updates (silent)
    checkForUpdates();
  });

  await seedDataFiles();
  await startPython();
}

// ── Python backend ──────────────────────────────────────────────────
async function startPython() {
  const fsp = fs.promises;
  const dataDir = getDataDir();

  if (isPackaged) {
    // Packaged: run the bundled engine executable
    const ext = process.platform === 'win32' ? '.exe' : '';
    const enginePath = path.join(process.resourcesPath, 'engine' + ext);

    // Ensure engine is executable on Linux
    if (process.platform !== 'win32') {
      try { await fsp.chmod(enginePath, 0o755); } catch (e) {}
    }

    // Ensure data dir exists
    await fsp.mkdir(dataDir, { recursive: true });

    const utf8Env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
    pyProcess = spawn(enginePath, ['--datadir', dataDir], {
      cwd: dataDir,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: utf8Env,
    });
    console.log('[Main] Spawned packaged engine:', enginePath, '--datadir', dataDir);
  } else {
    // Dev: run python engine.py as before
    const script = path.join(__dirname, 'backend', 'engine.py');
    const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
    const utf8Env = { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' };
    pyProcess = spawn(pythonCmd, [script, '--datadir', __dirname], {
      cwd: __dirname,
      stdio: ['pipe', 'pipe', 'pipe'],
      env: utf8Env,
    });
  }

  pyProcess.stdin.setDefaultEncoding('utf8');
  pyProcess.stdout.setEncoding('utf8');
  pyProcess.stderr.setEncoding('utf8');

  let buffer = '';
  let ready = false;
  const BUFFER_MAX = 1024 * 1024; // 1 MB safety cap
  pyProcess.stdout.on('data', (chunk) => {
    buffer += chunk;
    if (buffer.length > BUFFER_MAX) {
      console.error('[Main] Python stdout buffer exceeded 1MB — discarding');
      buffer = '';
      return;
    }
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
            if (housingCacheStale) {
              pyProcess.stdin.write('FETCH_HOUSING_CATALOG:eu\n');
            }
          }
        } catch {}
      }
    });
  });

  pyProcess.stderr.on('data', (d) => console.error('[Python]', d.trim()));
  pyProcess.on('close', (code) => {
    console.log('[Python] Exited with code', code);
    if (code !== 0 && code !== null) {
      win?.webContents.send('from-python', JSON.stringify({
        status: 'backend_crashed', code
      }));
    }
  });
}

// ── IPC ─────────────────────────────────────────────────────────────

let _buildsSaveQueue = Promise.resolve(); // serialize build saves to prevent corruption

ipcMain.on('to-python', (_, cmd) => {
  // Intercept build-string saves (handled by main, not Python)
  if (cmd.startsWith('SAVE_BUILD_STRING:')) {
    // Format: SAVE_BUILD_STRING:class:spec:type:string
    const parts = cmd.split(':');
    if (parts.length >= 5) {
      const [, classSlug, specSlug, buildType, ...rest] = parts;
      const buildString = rest.join(':'); // in case string contains ':'
      const buildsFile = getBuildsFile();
      _buildsSaveQueue = _buildsSaveQueue.then(async () => {
        try {
          if (!_buildsCache) _buildsCache = {};
          if (!_buildsCache[classSlug]) _buildsCache[classSlug] = {};
          if (!_buildsCache[classSlug][specSlug]) _buildsCache[classSlug][specSlug] = {};
          _buildsCache[classSlug][specSlug][buildType] = buildString;
          await fs.promises.writeFile(buildsFile, JSON.stringify(_buildsCache, null, 2));
          console.log(`[Main] Saved build string: ${classSlug}/${specSlug}/${buildType}`);
        } catch (e) {
          console.error('[Main] Error saving build string:', e.message);
        }
      });
      win?.webContents.send('from-python', JSON.stringify({
        status: 'build_string_saved',
        class_slug: classSlug, spec_slug: specSlug, build_type: buildType
      }));
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
ipcMain.on('show-data-folder', () => {
  shell.openPath(getDataDir());
});
ipcMain.on('set-resolution', (_, w, h) => {
  if (win && Number.isFinite(w) && Number.isFinite(h) && w >= 800 && h >= 600 && w <= 3840 && h <= 2160) {
    win.unmaximize();
    win.setSize(w, h);
    win.center();
  }
});

// ── Update check ────────────────────────────────────────────────────
function checkForUpdates() {
  const options = {
    hostname: 'api.github.com',
    path: '/repos/eightmouse/Innkeeper/releases/latest',
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
