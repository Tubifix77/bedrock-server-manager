# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python/tkinter desktop GUI for managing Minecraft Bedrock Dedicated Servers —
**one or many**, on this machine or across the LAN ("Majordomo", 2.0). Update, backup/restore,
run/console, world & `server.properties` editing, players, gamerules; a sidebar lists every
configured Server (local and remote) plus a Fleet overview. Despite the `_linux` filename, the
code is genuinely cross-platform — every OS-specific operation branches on `sys.platform ==
"win32"`.

All application logic lives in [`bedrock_updater_linux.py`](bedrock_updater_linux.py)
(~4400 lines). The other files are a desktop launcher, packaging scripts, docs, and license.
The same file also runs headless as a remote-administration host: `python3
bedrock_updater_linux.py --agent [--config PATH] [--port N]` — no tkinter import required
(guarded behind `TK_AVAILABLE`).

**Terminology (used in the GUI and all discussions — see [docs/GUI-DESIGN.md](docs/GUI-DESIGN.md)):**
a **Machine** hosts **Servers**; a Server is one configured install (name + port + config) that
holds **Worlds**; exactly one World is **Active** (`level-name`); the **Bedrock Server Version**
is the engine build that Update swaps; an **Administrator** is whichever instance is connected
*to* a Machine to control it. Don't say "server" for the engine files.

## Running

```bash
# Linux deps (stdlib + tkinter only — no pip packages, no requirements.txt)
sudo apt install python3 python3-tk -y
python3 bedrock_updater_linux.py
```

Requires Python 3.11+. There is **no build step, no committed test suite, and no
linter/formatter config** — do not look for `pytest`, `package.json`, CI, etc. Changes are
verified by running the GUI (and, when adding to the network layer, by writing a throwaway
scripted test in a scratch dir against a real `RemoteAdminHost`/`MachineConnection` — that's
how Stages 2-4 were built and verified; none of those scripts are kept in the repo). The app
needs a real bedrock server folder (containing `bedrock_server[.exe]` / `server.properties`)
to exercise most local code paths; exercising the remote path needs a second Machine (or a
loopback `--agent` on the same box) paired via Settings ▸ Remote Administration.

## Architecture

The file is divided by `# ===` banner comments into these sections, top to bottom:

- **Config & constants** — `DEFAULT_SETTINGS`, `DEFAULT_PRESERVE_ITEMS`, `SERVER_SIGNATURE_FILES`,
  `SERVER_EXECUTABLE` (`bedrock_server.exe` on Windows, `bedrock_server` elsewhere), and the
  remote-admin constants (`REMOTE_PROTO_VERSION`, `REMOTE_DEFAULT_PORT`, `MAX_MESSAGE_BYTES`).
  Config is **v2**: `server_profiles` (dict, keyed by profile id) + `active_profile` replaced
  the old single `last_server_path`; `machines` + `remote_admin` are new. `migrate_config_to_v2()`
  upgrades an old flat (pre-2.0) config once; `hydrate_active_profile_cache()` repopulates a set
  of flat "current profile" keys from the active profile on every load — most tab code still
  reads those flat keys (`self.config["preserve_items"]`, `self.server_entry`, etc.) unchanged,
  they're just backed by a profile now instead of a single global.
- **Utility functions** — pure helpers: cross-platform config/log paths, validation
  (`is_valid_bedrock_server`, `is_valid_bedrock_zip`), `make_executable`, `open_folder`,
  `parse_server_properties`/`save_server_properties` (module-level so the headless agent can
  use them too — `BedrockUpdaterApp` has thin delegating methods of the same name), etc.
- **`BackupManager`** — create/list/restore/cleanup backups, namespaced per-Server since 2.0
  at `<server_parent>/bedrock_backups/<server_dir_name>/` (an older flat
  `<server_parent>/bedrock_backups/` is still read/counted for rollback safety, never moved).
  Backups are either a copied folder or a `.zip` and contain only the user-selected "preserve" items.
- **`ServerManager`** — wraps the server as a `subprocess.Popen`. Uses an
  **observer/callback pattern**: register `output_callbacks` / `status_callbacks`; a daemon
  thread reads stdout and fans lines out to callbacks. Commands are sent via stdin
  (`send_command`), graceful stop sends the `stop` command then falls back to kill.
- **`ServerService`** — one Server's operations with **no tkinter dependency**: owns a
  `ServerManager` + `BackupManager` + a console ring buffer (`deque`), and exposes process
  control, reads (`get_info`/`list_worlds`/`read_properties`/`read_gamerules`/`get_players`),
  writes (`write_properties`/`set_active_world`/`rename_world`/`delete_world`/allowlist/
  permission/gamerule/gamemode), and backups. Mutating process ops go through a per-Server
  lock. This is the seam both the GUI and the remote-admin host operate through — `local click`
  and `remote click` hit the identical code and (in-process) the identical `ServerManager`.
- **Remote administration (LAN-only)** — `FramedConnection` (JSON-Lines over a socket) +
  `compute_auth`/`verify_auth` (HMAC pairing-token challenge) are the shared wire primitives.
  `RemoteAdminHost` is the host side: a threaded TCP server (accept thread; per-connection
  reader+writer threads joined by a queue, so the writer is the sole socket writer) that
  authenticates a connection, dispatches ops to a `ServerService`, runs long ops
  (`stop`/`restart`/`create_backup`/`restore_backup`) in worker threads, and fans out events
  (console, status, `servers_changed`, progress) with console-burst coalescing.
  `MachineConnection` is the administrator side: one persistent, auto-reconnecting connection
  per remote Machine, with a worker thread that owns the socket and `request(op, ...)` calls
  from any thread blocking on an id-correlated result slot. **Its callbacks fire on the worker
  thread — never call `root.after()` directly from them; enqueue onto `self._remote_queue`
  and let `_drain_remote_queue()` (a `root.after`-rescheduled poller on the main thread) apply
  them.** `RemoteServerAccess` mirrors `ServerService`'s whole method surface, each call
  becoming `connection.request(op, server, params)`.
  `AgentApp` is the headless (`--agent`) host: same provider interface as `BedrockUpdaterApp`,
  no widgets. Remote-triggered **Update stays local-only, by design** (see below), as do
  physically-local actions (opening a folder, backup cleanup) — guarded via `_block_if_remote()`.
- **`BedrockUpdaterApp`** — the main window. A resizable sidebar (Machines → Servers, plus a
  🌐 Fleet root) sits left of a 7-tab `ttk.Notebook`; selecting a Server points every tab at
  `self.active_access` (a local `ServerService` or a `RemoteServerAccess` — **tabs call this,
  never `ServerManager`/file paths directly**, which is what makes them remote-capable at all).
  Selecting the Fleet root or a Machine node hides the notebook and shows `self.overview_frame`
  instead (`show_notebook()`/`show_overview()`), rebuilt live by `build_fleet_overview()`/
  `build_machine_page()`. `self.contexts` is the per-profile `ServerService` registry — a
  Server keeps running there even when you're viewing a different one or the Fleet.
  Tabs, in order: **Server** (home: Active Server Information, Active World dropdown,
  start/stop, 🎲 Gamerules dialog, console) / **Worlds** (create, rename, delete, per-World
  last-run version) / **Players** (allowlist + roles + per-player gamemode; names/XUIDs
  harvested from join lines into the `known_players` config key) / **Configuration** (the
  properties editor; page header "Active Server Configuration") / **Backups** (preserve
  checklist + per-Server backup policy + backup list, header names the Server) / **Update**
  (Bedrock Server Version tools — local-only) / **Settings** (app prefs, Current Server's
  Folder relocator, Remote Administration toggle/port/token).
  Three config systems, kept distinct: `server.properties` (Configuration), per-World
  **gamerules** in `level.dat` (set live via `gamerule` console command), and the player JSON
  files (`allowlist.json`/`permissions.json`). Bedrock has no blacklist.
- **`ServerPropertiesEditor`** — a `ttk.Frame` subclass (falls back to `object` when tkinter
  is absent — `_TkFrameBase` — so the class statement doesn't crash on import under `--agent`)
  that renders `server.properties` as editable key/value rows (skips `level-name`, owned by the
  Active World dropdown on Server) via `self.app.active_access`. Tracks a load/save snapshot
  (`has_unsaved_changes()`) so switching Servers can warn before discarding edits.
- **World versions** — `get_world_last_opened_version()` reads the `lastOpenedWithVersion`
  NBT tag straight out of a world's binary `level.dat` (little-endian byte scan, no NBT lib);
  a World won't load on a Bedrock Server Version older than that stamp.

### Patterns that govern almost every change

1. **Threading / UI marshaling.** Every slow operation (update, backup, restore, download,
   server stop/restart) runs in a `threading.Thread(daemon=True)`. tkinter is single-threaded,
   so worker threads **must not touch widgets directly**. Two valid patterns, don't mix them up:
   - *Local* work (ServerManager's reader thread, update/backup workers): `self.root.after(0,
     lambda: ...)` directly from the worker. This is correct and must stay as-is — it only
     appears to fail under a headless `update()`-loop test harness (no real `mainloop()`); it
     works fine in the actual running app.
   - *Remote* work (`MachineConnection` callbacks): don't call `root.after()` from them —
     enqueue onto `self._remote_queue` and let the main-thread poller (`_drain_remote_queue`)
     apply it instead. Both patterns work correctly under the app's real `mainloop()`; the
     queue was adopted for remote specifically because it's what let this code be verified with
     headless scripted tests (no `mainloop()` running) — keep using it there so that stays true.

2. **The update pipeline is destructive.** `perform_update()` does:
   backup preserved items → **delete the entire server folder contents** → extract the new ZIP →
   restore preserved items from the just-made backup → optionally trim old backups. The
   "preserve list" (the *What to back up* checkboxes on the **Backups** tab, defaults from
   `DEFAULT_PRESERVE_ITEMS`) is the only thing standing between an update and total data loss —
   `worlds` is flagged `critical`. A "fresh install" (no existing `server.properties`) skips
   the backup and wipe steps. **This is why Update is local-only**: running something this
   destructive blind over a network link, where a dropped connection mid-copy could leave a
   Server half-wiped with no one there to notice, isn't a risk worth taking — don't wire it
   remotely without a much better story than "add a host op".

For **GUI/UX changes**, follow [docs/GUI-DESIGN.md](docs/GUI-DESIGN.md) — it's the source of
truth for tab roles, terminology, sidebar/Fleet behavior, and the conventions the app now holds
to: *confirmed-only* Server side-effects (a click may stop/start the Server only right after a
yes/no dialog, and now names the Machine too — the Active World dropdown greys out instead),
dynamic tabs self-refreshing on open, and marking the Active World (✅). Every setting saved to
config also needs its default in `DEFAULT_SETTINGS` (or, if per-Server, in the profile shape
`add_server_profile()`/`migrate_config_to_v2()` build).

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

`load_config()` deep-merges the saved JSON over `DEFAULT_SETTINGS`, migrates a pre-2.0 flat
config to v2 (`migrate_config_to_v2()`, keeping a `*.v1.bak`) if needed, then hydrates the flat
"current profile" keys from `server_profiles[active_profile]` (`hydrate_active_profile_cache()`).
Adding a new **app-level** setting means adding its default to `DEFAULT_SETTINGS`; adding a new
**per-Server** setting means adding it to the profile shape in both `migrate_config_to_v2()`
and `add_server_profile()`, and to the sync in `_sync_flat_settings_into_active_profile()`.
State is written back in `on_close()` and via each tab's Save button
(`save_settings()` is location-agnostic — it just reads whichever widgets currently hold its
`tk.Variable`s, regardless of which tab they're gridded into).

## Gotchas

- [`bedrock-server-manager.desktop`](bedrock-server-manager.desktop) has **hardcoded paths**
  (`/home/boas/Bedrock/...`) — they're user-specific and must be edited per install.
- Update checking is manual: the app builds a guessed download URL from a user-entered version
  string (`manual_version_input`) rather than querying an API, because minecraft.net has no
  stable version endpoint.
- Remote administration is **LAN-only by design** — plaintext session after an HMAC-authenticated
  handshake, no TLS. Don't add internet-exposure features (port-forwarding helpers, etc.)
  without a real security redesign; the documented answer for reaching a Machine off-LAN is a
  VPN (Tailscale/WireGuard), not exposing the port.
- `self.contexts` (local profile_id → `ServerService`) and `self.connections` (machine_id →
  `MachineConnection`) are both long-lived registries the GUI never tears down on its own —
  only `on_close()` stops everything. A Server left running while you view something else, or
  a Machine connection left open while you're not looking at it, is intentional, not a leak.
