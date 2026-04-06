#!/bin/bash
# Build QueryLux desktop app for macOS / Linux
# Usage: ./build-desktop.sh [mac|linux|all]
set -e

TARGET=${1:-mac}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║     QueryLux Desktop Build               ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── 1. Bundle Python server ──────────────────────────────────────────────────
echo "▶ Step 1/3: Bundling Python server with PyInstaller…"
pip install pyinstaller --quiet
pip install -r requirements.txt --quiet
pyinstaller server.spec --clean --noconfirm --distpath dist-server
echo "  ✓ Server bundled → dist-server/server/"

# ── 2. Install Electron deps ─────────────────────────────────────────────────
echo ""
echo "▶ Step 2/3: Installing Electron dependencies…"
cd electron && npm install --silent && cd ..
echo "  ✓ npm install done"

# ── 3. Build Electron app ────────────────────────────────────────────────────
echo ""
echo "▶ Step 3/3: Building Electron app…"
cd electron

case "$TARGET" in
  mac)   npm run build:mac ;;
  linux) npm run build:linux ;;
  all)   npm run build:all ;;
  *)     echo "Unknown target: $TARGET (use mac|linux|all)"; exit 1 ;;
esac

cd ..

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║  ✓ Build complete!                       ║"
echo "║  Output: dist-electron/                  ║"
echo "╚══════════════════════════════════════════╝"
ls -lh dist-electron/ 2>/dev/null || true
