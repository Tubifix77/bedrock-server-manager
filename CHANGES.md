# Changelog

All notable changes to this project will be documented here.

---

## [1.0.4] — 2026-07-13

Player management and per-World gamerules — the two systems that live *outside*
`server.properties` and therefore couldn't be reached from the Configuration tab.

### Added
- **👥 Players tab** (after Worlds):
  - **Access** — allowlist editor (`allowlist.json`) with a master *"restrict joining to this
    list"* toggle that also writes `allow-list` (and applies live via `allowlist on/off` when
    the Server runs). Turning it on with an empty list warns first. Includes the honest note
    that Bedrock has **no blacklist** — exclusion = allowlist without that player.
  - **Roles** — `permissions.json` editor: set visitor / member / operator per player
    (applies live via `permission reload`).
  - **Per-player game mode** — pick a player, click Survival / Creative / Adventure; sent live
    to the running Server so different players can play different modes on the same Server.
    Warns if `force-gamemode=true` would defeat mixed modes.
  - Player names + XUIDs are **learned automatically** from "Player connected" console lines
    (stored as `known_players` in the app config).
- **🎲 Gamerules dialog** (Server tab) — reads the Active World's current gamerules straight
  from `level.dat` (sleep percentage, keepinventory, mobgriefing, daylight/weather cycle,
  fire tick, TNT, insomnia/phantoms, PvP, coordinates, fall damage) and applies changes live
  via `gamerule` console commands. `playerssleepingpercentage 0` = one sleeper skips the night.

---

## [1.0.3] — 2026-07-13

GUI restructured around a clear model: a **Server** (one install, one port) holds **Worlds**;
one World is **Active**; the **Bedrock Server Version** is what Update swaps.
Full layout rationale in [`docs/GUI-DESIGN.md`](docs/GUI-DESIGN.md).

### Added
- **Active World dropdown** on the Server tab — switch Worlds safely (existing folders only).
  Greys out while the Server runs, with an inline *"Not available until the running Server is
  stopped"* hint that disappears when stopped. Kills the old free-text typo trap.
- **Create New World** flow on the Worlds tab, plus **rename** and **delete** (both refuse the
  Active World / a running Server). A new World appears immediately as an orange pending row
  (*"created on next start — configure, then Start"*), the app offers to name the Server after
  it while the stock "Dedicated Server" name is unchanged, and then jumps straight to
  Active Server Configuration.
- **Set as Active World** on the Worlds tab can switch while the Server runs: after a
  confirmation it stops the Server nicely, switches, and starts it again on the new World.
  When the Server is stopped it switches instantly and confirms it did. The Active World is
  now marked **✅ ACTIVE** (bold row, same font) in the Worlds list; on the Server tab the
  Active World is simply the one shown in the dropdown, so it's always obvious which World is
  current.
- `server-name` in Active Server Configuration now carries a hint: *(the name players see in
  their server list)*.
- **Update stops the Server nicely, in the background** — the graceful stop now runs inside
  the update worker thread (the UI used to freeze for up to 30 s), and the confirmation
  dialog now says the running Server will be stopped first (and restarted afterwards, when
  that setting is enabled).
- **Tabs auto-refresh when opened** — the Worlds list and the Server tab's info panel re-scan
  on every visit, so a freshly generated World replaces its pending row without pressing
  Refresh. (Active Server Configuration deliberately doesn't auto-reload — unsaved edits
  survive tab hopping.)
- **Per-World "Last Run On" version** — read from each World's `level.dat`
  (`lastOpenedWithVersion` NBT tag), shown in the Worlds list and in Active Server Information
  with a *"won't load on older versions"* note and a ⚠ when a World is newer than the
  installed Bedrock Server Version.
- **Backups header** names the Server the backups belong to and where they're stored.

### Changed
- **Tab order:** Server (home) → Worlds → Active Server Configuration → Backups → Update → Settings.
- **Server Information** moved from Update to the Server tab (now *Active Server Information*);
  console height halved to make room.
- **"What to back up" checklist** (the preserve list) moved from Update to Backups — it's the
  same list updates preserve.
- **Server Folder picker** moved from Update to Settings ("Server Location") — one-time setup.
- **Properties tab** renamed: the tab reads **Configuration**; the page header carries the
  full **Active Server Configuration**.
- Update tab slimmed to the version tools; shows the installed Bedrock Server Version;
  dark-mode toggle moved to Settings; fresh installs now point new users to the Worlds tab.

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
