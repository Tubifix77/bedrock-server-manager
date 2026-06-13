#!/usr/bin/env bash
# Build a Linux x86_64 AppImage for Bedrock Server Manager.
# Requires: python3 with tkinter, `pip install pyinstaller`, curl.
# Run on an x86_64 Linux host (use an older distro like Ubuntu 22.04 for broad glibc compatibility).
# Usage:  packaging/linux/build-appimage.sh [version]
set -euo pipefail

VERSION="${1:-1.0.2}"
APP=BedrockServerManager
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

# 1. PyInstaller one-folder build (bundles Python + Tcl/Tk).
python3 -m PyInstaller bedrock_updater.spec --noconfirm --clean

# 2. Assemble the AppDir.
rm -rf AppDir
mkdir -p AppDir/usr/bin \
         AppDir/usr/share/applications \
         AppDir/usr/share/icons/hicolor/128x128/apps
cp -a "dist/$APP/." AppDir/usr/bin/
install -m 0755 "$HERE/AppRun" AppDir/AppRun
cp "$HERE/bedrock-server-manager.desktop" AppDir/bedrock-server-manager.desktop
cp "$HERE/bedrock-server-manager.desktop" AppDir/usr/share/applications/
cp minecraft.png AppDir/minecraft.png
cp minecraft.png AppDir/usr/share/icons/hicolor/128x128/apps/minecraft.png

# 3. Fetch appimagetool (continuous build) if not already present.
TOOL=appimagetool-x86_64.AppImage
if [ ! -x "$TOOL" ]; then
  curl -fL -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

# 4. Build the AppImage. extract-and-run avoids needing FUSE (e.g. in CI/containers).
export APPIMAGE_EXTRACT_AND_RUN=1
ARCH=x86_64 "./$TOOL" AppDir "$APP-$VERSION-x86_64.AppImage"
echo "Built $APP-$VERSION-x86_64.AppImage"
