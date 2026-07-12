# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python/tkinter desktop GUI for managing Minecraft Bedrock Dedicated Servers
(update, backup/restore, run/console, world & `server.properties` editing). Despite the
`_linux` filename, the code is genuinely cross-platform — every OS-specific operation
branches on `sys.platform == "win32"`.

All application logic lives in [`bedrock_updater_linux.py`](bedrock_updater_linux.py) (~2000 lines).
The other files are a desktop launcher, packaging scripts, docs, and license.

**Terminology (used in the GUI and all discussions — see [docs/GUI-DESIGN.md](docs/GUI-DESIGN.md)):**
a **Server** is one configured install (name + port + config) that holds **Worlds**; exactly one
World is **Active** (`level-name`); the **Bedrock Server Version** is the engine build that
Update swaps. Don't say "server" for the engine files.

## Running

```bash
# Linux deps (stdlib + tkinter only — no pip packages, no requirements.txt)
sudo apt install python3 python3-tk -y
python3 bedrock_updater_linux.py
```

Requires Python 3.11+. There is **no build step, no test suite, and no linter/formatter
config** — do not look for `pytest`, `package.json`, CI, etc. Changes are verified by running
the GUI. The app needs a real bedrock server folder (containing `bedrock_server[.exe]` /
`server.properties`) to exercise most code paths.

## Architecture

The file is divided by `# ===` banner comments into these sections, top to bottom:

- **Config & constants** — `DEFAULT_SETTINGS`, `DEFAULT_PRESERVE_ITEMS`, `SERVER_SIGNATURE_FILES`,
  and `SERVER_EXECUTABLE` (`bedrock_server.exe` on Windows, `bedrock_server` elsewhere).
- **Utility functions** — pure helpers: cross-platform config/log paths, validation
  (`is_valid_bedrock_server`, `is_valid_bedrock_zip`), `make_executable`, `open_folder`, etc.
- **`BackupManager`** — create/list/restore/cleanup backups in `<server_parent>/bedrock_backups/`.
  Backups are either a copied folder or a `.zip` (controlled by the `compress_backups` setting)
  and contain only the user-selected "preserve" items.
- **`ServerManager`** — wraps the server as a `subprocess.Popen`. Uses an
  **observer/callback pattern**: register `output_callbacks` / `status_callbacks`; a daemon
  thread reads stdout and fans lines out to callbacks. Commands are sent via stdin
  (`send_command`), graceful stop sends the `stop` command then falls back to kill.
- **`BedrockUpdaterApp`** — the main window. Builds a 6-tab `ttk.Notebook` in this order:
  **Server** (home: Active Server Information, Active World dropdown, start/stop, console) /
  **Worlds** (create, rename, delete, per-World last-run version) /
  **Active Server Configuration** (the properties editor) / **Backups** (preserve checklist +
  backup list, header names the Server) / **Update** (Bedrock Server Version tools) /
  **Settings** (app prefs + the Server Folder picker, `self.server_entry`).
- **`ServerPropertiesEditor`** — a `ttk.Frame` subclass that renders `server.properties` as
  editable key/value rows (skips `level-name`, owned by the Active World dropdown on Server).
- **World versions** — `get_world_last_opened_version()` reads the `lastOpenedWithVersion`
  NBT tag straight out of a world's binary `level.dat` (little-endian byte scan, no NBT lib);
  a World won't load on a Bedrock Server Version older than that stamp.

### Two patterns that govern almost every change

1. **Threading / UI marshaling.** Every slow operation (update, backup, restore, download,
   server stop/restart) runs in a `threading.Thread(daemon=True)`. tkinter is single-threaded,
   so worker threads **must not touch widgets directly** — they marshal back with
   `self.root.after(0, lambda: ...)`. Follow this when adding any long-running work.

2. **The update pipeline is destructive.** `perform_update()` does:
   backup preserved items → **delete the entire server folder contents** → extract the new ZIP →
   restore preserved items from the just-made backup → optionally trim old backups. The
   "preserve list" (the *What to back up* checkboxes on the **Backups** tab, defaults from
   `DEFAULT_PRESERVE_ITEMS`) is the only thing standing between an update and total data loss —
   `worlds` is flagged `critical`. A "fresh install" (no existing `server.properties`) skips
   the backup and wipe steps.

### Cross-platform touch points

When changing OS-specific behavior, these are the spots that branch on `win32` and usually need
updating together:

- `SERVER_EXECUTABLE` constant and `ServerManager.start()` — Windows uses `CREATE_NO_WINDOW`;
  Linux/macOS set `LD_LIBRARY_PATH` to the server dir and `chmod` the executable first.
- `get_config_path()` / `get_log_dir()` — `%APPDATA%` on Windows vs XDG paths
  (`~/.config/bedrock-updater`, `~/.local/share/bedrock-updater/logs`) elsewhere.
- `get_downloads_folder()` — Windows registry vs XDG `user-dirs.dirs`.
- `open_folder()` — `os.startfile` / `open` / `xdg-open`.

### Config & persistence

`load_config()` deep-merges the saved JSON over `DEFAULT_SETTINGS`, so adding a new setting means
adding its default there. State (window geometry, last paths, per-item preserve toggles) is
written back in `on_close()` and via the Settings tab's Save button.

## Gotchas

- [`bedrock-server-manager.desktop`](bedrock-server-manager.desktop) has **hardcoded paths**
  (`/home/boas/Bedrock/...`) — they're user-specific and must be edited per install.
- Update checking is manual: the app builds a guessed download URL from a user-entered version
  string (`manual_version_input`) rather than querying an API, because minecraft.net has no
  stable version endpoint.
