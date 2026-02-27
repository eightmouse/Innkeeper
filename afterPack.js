const path = require('path');
const fs = require('fs');
const { execFileSync } = require('child_process');

exports.default = async function (context) {
  if (process.platform !== 'win32') return;

  const exePath = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  const iconPath = path.join(__dirname, 'assets', 'icon.ico');

  if (!fs.existsSync(iconPath)) {
    console.log('  • icon.ico not found, skipping');
    return;
  }

  // Find rcedit: check electron-builder's cache
  const cacheDir = path.join(require('os').homedir(), 'AppData', 'Local', 'electron-builder', 'Cache', 'winCodeSign');
  let rceditPath = null;

  try {
    if (fs.existsSync(cacheDir)) {
      const entries = fs.readdirSync(cacheDir).filter(e => e.startsWith('winCodeSign'));
      for (const entry of entries) {
        const candidate = path.join(cacheDir, entry, 'rcedit-x64.exe');
        if (fs.existsSync(candidate)) {
          rceditPath = candidate;
          break;
        }
      }
    }
  } catch (e) {
    // Cache dir doesn't exist or isn't readable — that's fine
  }

  if (!rceditPath) {
    console.log('  • rcedit not found, skipping icon set');
    return;
  }

  try {
    execFileSync(rceditPath, [exePath, '--set-icon', iconPath]);
    console.log('  • icon set successfully');
  } catch (e) {
    console.error('  • failed to set icon:', e.message);
  }
};
