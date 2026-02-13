const { app, BrowserWindow, ipcMain, shell } = require('electron');
const path = require('path');
const { spawn } = require('child_process');

let win;
let pyProcess;

function createWindow() {
  win = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1200,
    minHeight: 800,
    frame: false,
    backgroundColor: '#080e0a',
    icon: path.join(__dirname, 'icon.png'),
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });

  win.loadFile('index.html');
  startPython();
}

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

  pyProcess.stderr.on('data', (d) => console.error('[Python ERR]', d.toString().trim()));
  pyProcess.on('close', (code) => console.log('[Python] Process exited with code', code));

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

  // Mirror from-python to internal listener too
  pyProcess.stdout.on('data', (chunk) => {
    chunk.toString().split('\n').forEach(line => {
      line = line.trim();
      if (line) ipcMain.emit('from-python-internal', null, line);
    });
  });
}

// Forward renderer → python
ipcMain.on('to-python', (_, cmd) => {
  pyProcess?.stdin.write(cmd + '\n');
});

// Window controls
ipcMain.on('window-close',    () => win?.close());
ipcMain.on('window-minimize', () => win?.minimize());
ipcMain.on('window-maximize', () => win?.isMaximized() ? win.unmaximize() : win.maximize());

// Open links in real browser
ipcMain.on('open-external', (_, url) => shell.openExternal(url));

app.whenReady().then(createWindow);

app.on('window-all-closed', () => {
  pyProcess?.stdin.write('EXIT\n');
  if (process.platform !== 'darwin') app.quit();
});