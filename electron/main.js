'use strict'

const {
  app, BrowserWindow, shell, Menu, Tray,
  nativeImage, ipcMain, dialog
} = require('electron')
const { spawn }  = require('child_process')
const http       = require('http')
const path       = require('path')
const net        = require('net')
const fs         = require('fs')
const os         = require('os')

// ── Constants ──────────────────────────────────────────────────────────────
const IS_DEV     = !app.isPackaged
const IS_MAC     = process.platform === 'darwin'
const IS_WIN     = process.platform === 'win32'
const APP_NAME   = 'QueryLux'

let mainWindow   = null
let splashWindow = null
let serverProc   = null
let tray         = null
let serverPort   = 0

// ── Find a free TCP port ───────────────────────────────────────────────────
function findFreePort () {
  return new Promise((resolve, reject) => {
    const srv = net.createServer()
    srv.unref()
    srv.on('error', reject)
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address()
      srv.close(() => resolve(port))
    })
  })
}

// ── Poll /health until server responds ────────────────────────────────────
function waitForServer (port, timeoutMs = 45000) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs
    const attempt  = () => {
      http.get(`http://127.0.0.1:${port}/health`, res => {
        let body = ''
        res.on('data', d => body += d)
        res.on('end', () => {
          try {
            const j = JSON.parse(body)
            if (j.status === 'ok') return resolve()
          } catch {}
          retry()
        })
      }).on('error', retry)
    }
    const retry = () => {
      if (Date.now() > deadline) return reject(new Error('Server did not start in time'))
      setTimeout(attempt, 400)
    }
    attempt()
  })
}

// ── Locate the bundled Python server binary ────────────────────────────────
function getServerPath () {
  if (IS_DEV) {
    // Dev: run python directly from repo root
    const repoRoot = path.join(__dirname, '..')
    const py = IS_WIN ? 'python' : 'python3'
    return { cmd: py, args: [path.join(repoRoot, 'server.py')], cwd: repoRoot }
  }

  // Production: PyInstaller binary in resources/server/
  const resourcesDir = process.resourcesPath
  const serverDir    = path.join(resourcesDir, 'server')
  const bin          = IS_WIN ? 'server.exe' : 'server'
  const binPath      = path.join(serverDir, bin)

  if (!fs.existsSync(binPath)) {
    dialog.showErrorBox(APP_NAME, `Server binary not found:\n${binPath}`)
    app.quit()
  }

  return { cmd: binPath, args: [], cwd: serverDir }
}

// ── User data directory (persists across app updates) ─────────────────────
function getUserDataDir () {
  const dir = path.join(app.getPath('userData'), 'data')
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

// ── Start the Python backend ───────────────────────────────────────────────
async function startServer () {
  serverPort = await findFreePort()
  const { cmd, args, cwd } = getServerPath()
  const dataDir = getUserDataDir()

  const env = {
    ...process.env,
    PORT:         String(serverPort),
    DATA_DIR:     dataDir,
    DESKTOP_MODE: '1',          // disables Firebase auth — local-only mode
    HOST:         '127.0.0.1',  // never expose on network
  }

  // Remove any Railway/cloud env vars that shouldn't run locally
  delete env.RAILWAY_ENVIRONMENT
  delete env.FIREBASE_PROJECT_ID
  delete env.FIREBASE_SERVICE_ACCOUNT_JSON

  console.log(`[server] Starting: ${cmd} ${args.join(' ')}`)
  console.log(`[server] Port: ${serverPort}  DATA_DIR: ${dataDir}`)

  serverProc = spawn(cmd, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] })

  serverProc.stdout.on('data', d => console.log('[py]', d.toString().trim()))
  serverProc.stderr.on('data', d => console.error('[py]', d.toString().trim()))

  serverProc.on('exit', (code, signal) => {
    console.log(`[server] exited code=${code} signal=${signal}`)
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showErrorBox(APP_NAME, 'The backend server stopped unexpectedly. Please restart the app.')
      app.quit()
    }
  })
}

// ── Create splash screen ───────────────────────────────────────────────────
function createSplash () {
  splashWindow = new BrowserWindow({
    width:           480,
    height:          320,
    frame:           false,
    transparent:     false,
    resizable:       false,
    center:          true,
    alwaysOnTop:     true,
    backgroundColor: '#1e1e2e',
    webPreferences:  { nodeIntegration: true, contextIsolation: false },
  })
  splashWindow.loadFile(path.join(__dirname, 'splash.html'))
  splashWindow.webContents.on('did-finish-load', () => {
    splashWindow.webContents.send('version', app.getVersion())
  })
}

function setSplashStatus (msg) {
  if (splashWindow && !splashWindow.isDestroyed()) {
    splashWindow.webContents.send('status', msg)
  }
}

// ── Create main window ─────────────────────────────────────────────────────
function createMainWindow () {
  mainWindow = new BrowserWindow({
    width:           1400,
    height:          900,
    minWidth:        900,
    minHeight:       600,
    show:            false,
    backgroundColor: '#1e1e2e',
    titleBarStyle:   IS_MAC ? 'hiddenInset' : 'default',
    icon:            path.join(__dirname, 'assets', IS_WIN ? 'icon.ico' : 'icon.png'),
    webPreferences: {
      preload:            path.join(__dirname, 'preload.js'),
      contextIsolation:   true,
      nodeIntegration:    false,
      webSecurity:        true,
      allowRunningInsecureContent: false,
    },
  })

  mainWindow.loadURL(`http://127.0.0.1:${serverPort}`)

  mainWindow.once('ready-to-show', () => {
    if (splashWindow && !splashWindow.isDestroyed()) {
      splashWindow.destroy()
      splashWindow = null
    }
    mainWindow.show()
    if (IS_DEV) mainWindow.webContents.openDevTools()
  })

  mainWindow.on('closed', () => { mainWindow = null })

  // Open external links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })

  mainWindow.webContents.on('will-navigate', (e, url) => {
    if (!url.startsWith(`http://127.0.0.1:${serverPort}`)) {
      e.preventDefault()
      shell.openExternal(url)
    }
  })

  buildMenu()
}

// ── App menu ───────────────────────────────────────────────────────────────
function buildMenu () {
  const template = [
    ...(IS_MAC ? [{
      label: APP_NAME,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'services' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    }] : []),
    {
      label: 'File',
      submenu: [
        {
          label: 'New Connection',
          accelerator: 'CmdOrCtrl+N',
          click: () => mainWindow?.webContents.executeJavaScript('openConnModal()'),
        },
        { type: 'separator' },
        IS_MAC ? { role: 'close' } : { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' }, { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' }, { role: 'copy' }, { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(IS_DEV ? [{ type: 'separator' }, { role: 'toggleDevTools' }] : []),
      ],
    },
    {
      label: 'Window',
      submenu: [
        { role: 'minimize' },
        ...(IS_MAC ? [{ role: 'zoom' }, { type: 'separator' }, { role: 'front' }] : []),
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'QueryLux on GitHub',
          click: () => shell.openExternal('https://github.com/shaiksameer0045-dotcom/sql-agent'),
        },
        {
          label: 'Report an Issue',
          click: () => shell.openExternal('https://github.com/shaiksameer0045-dotcom/sql-agent/issues'),
        },
        { type: 'separator' },
        {
          label: `Version ${app.getVersion()}`,
          enabled: false,
        },
      ],
    },
  ]

  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ── System tray ────────────────────────────────────────────────────────────
function createTray () {
  const iconPath = path.join(__dirname, 'assets', IS_MAC ? 'icon.png' : IS_WIN ? 'icon.ico' : 'icon.png')
  const img = nativeImage.createFromPath(iconPath)
  // Mac tray icons should be template images (16×16)
  if (IS_MAC) img.setTemplateImage(true)

  tray = new Tray(img.resize({ width: 16, height: 16 }))
  tray.setToolTip(APP_NAME)

  const menu = Menu.buildFromTemplate([
    { label: APP_NAME, enabled: false },
    { type: 'separator' },
    { label: 'Show Window', click: () => { mainWindow?.show(); mainWindow?.focus() } },
    { type: 'separator' },
    { label: 'Quit', click: () => app.quit() },
  ])
  tray.setContextMenu(menu)
  tray.on('double-click', () => { mainWindow?.show(); mainWindow?.focus() })
}

// ── IPC handlers ───────────────────────────────────────────────────────────
ipcMain.handle('get-version',    () => app.getVersion())
ipcMain.handle('open-external',  (_, url) => shell.openExternal(url))

// ── App lifecycle ──────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  // Single instance lock
  if (!app.requestSingleInstanceLock()) {
    app.quit()
    return
  }

  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  createSplash()

  try {
    setSplashStatus('Starting Python server…')
    await startServer()

    setSplashStatus('Waiting for server to be ready…')
    await waitForServer(serverPort)

    setSplashStatus('Loading app…')
    createMainWindow()
    createTray()

  } catch (err) {
    console.error('Startup error:', err)
    dialog.showErrorBox(APP_NAME, `Failed to start: ${err.message}`)
    app.quit()
  }
})

app.on('window-all-closed', () => {
  // On Mac, keep running in tray unless explicitly quit
  if (!IS_MAC) app.quit()
})

app.on('activate', () => {
  if (!mainWindow) createMainWindow()
  else mainWindow.show()
})

app.on('before-quit', () => {
  // Kill Python server gracefully
  if (serverProc) {
    try {
      if (IS_WIN) {
        spawn('taskkill', ['/pid', serverProc.pid, '/f', '/t'])
      } else {
        serverProc.kill('SIGTERM')
        setTimeout(() => serverProc?.kill('SIGKILL'), 3000)
      }
    } catch {}
    serverProc = null
  }
})

// Security: prevent new window creation
app.on('web-contents-created', (_, contents) => {
  contents.on('will-attach-webview', e => e.preventDefault())
})
