# Changelog

All notable changes to this project will be documented here.

---

## [1.0.2] — 2026-06-14

### Added
- **Cross-platform packaging** — standalone installers requiring no Python on the user's machine:
  - Windows: PyInstaller + Inno Setup `.exe` installer (folder picker, Start-menu/desktop shortcuts)
  - Linux: PyInstaller + AppImage (run-anywhere)
- **GitHub Actions release workflow** — push a `v*` tag to automatically build and publish both installers as a GitHub Release
- **Taskbar/window icon** — app icon now appears correctly in the taskbar and window decorations on both platforms
- **Numbered update flow** — Update tab buttons now read as a guided sequence: `1: 🌐 Wiki Version → 2: ⬇️ Download Latest → 3: 🚀 Update Server`
- **Windows `.ico`** — multi-resolution icon (16–128px) generated from `minecraft.png`
- **Frozen-build icon path fix** — `sys._MEIPASS` support so the icon loads correctly inside a PyInstaller bundle

### Changed
- Version string cleaned up from `1.0.2-Linux` to `1.0.2` — the app is genuinely cross-platform
- `Dry Run` and `Open Folder` buttons moved to the right side of the Update tab, out of the main update flow
- `minecraft.png` updated to transparent background (was white)

---

## [1.0.1] — 2026-06-13

### Added
- Full Linux (Debian 12) compatibility:
  - Auto-sets `+x` permission on `bedrock_server` executable at startup, after updates, and after restores
  - Sets `LD_LIBRARY_PATH` to the server directory when launching the server process
  - XDG directory support for Downloads folder detection (`user-dirs.dirs`)
  - XDG config/log paths: `~/.config/bedrock-updater/` and `~/.local/share/bedrock-updater/logs/`
  - Uses `xdg-open` for file browser instead of `os.startfile`
- `.desktop` launcher for XFCE/GNOME/KDE desktop integration
- `minecraft.png` icon bundled with the app

### Changed
- `Path` objects used consistently throughout for cross-platform path handling
- Theme falls back to `clam` on Linux (replaces Windows-only `vista` theme)

---

## [1.0.0] — Initial release

- GUI application for managing Minecraft Bedrock Dedicated Servers on Windows
- Update server from ZIP with automatic backup and selective file preservation
- Backup management: create, restore, auto-cleanup, optional ZIP compression
- World browser: view worlds, sizes, last-modified dates, switch active world
- Server console: start/stop/restart server, send commands, live output
- `server.properties` editor
- Settings: max backups, compression, auto-start/stop, dark mode
