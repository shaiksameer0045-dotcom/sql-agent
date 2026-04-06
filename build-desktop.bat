@echo off
REM Build QueryLux desktop app for Windows
REM Usage: build-desktop.bat

echo.
echo ╔══════════════════════════════════════════╗
echo ║     QueryLux Desktop Build (Windows)     ║
echo ╚══════════════════════════════════════════╝
echo.

REM ── 1. Bundle Python server ────────────────────────────────────────────────
echo [1/3] Bundling Python server with PyInstaller...
pip install pyinstaller -q
pip install -r requirements.txt -q
pyinstaller server.spec --clean --noconfirm --distpath dist-server
if errorlevel 1 ( echo ERROR: PyInstaller failed & exit /b 1 )
echo   Done: dist-server\server\

REM ── 2. Electron deps ───────────────────────────────────────────────────────
echo.
echo [2/3] Installing Electron dependencies...
cd electron
npm install --silent
if errorlevel 1 ( echo ERROR: npm install failed & cd .. & exit /b 1 )
cd ..
echo   Done

REM ── 3. Build Electron ──────────────────────────────────────────────────────
echo.
echo [3/3] Building Windows installer...
cd electron
npm run build:win
if errorlevel 1 ( echo ERROR: Electron build failed & cd .. & exit /b 1 )
cd ..

echo.
echo ╔══════════════════════════════════════════╗
echo ║  Done! Output: dist-electron\            ║
echo ╚══════════════════════════════════════════╝
dir dist-electron\ 2>nul
