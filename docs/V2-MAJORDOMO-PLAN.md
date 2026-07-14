# Bedrock Server Manager 2.0 "Majordomo" — multi-Server, multi-Machine administration

> **STATUS (2026-07-14):** Planning finished and reviewed with the user. **No code written
> yet** — this branch (`v2-majordomo`) currently differs from `master` only by this planning
> document. Implementation is deliberately deferred.
>
> **Branching decision:** 2.0 lives on the `v2-majordomo` branch of *this same repo*, not a
> separate repo — 2.0 is a refactor of the same single file (one file is both host and
> administrator; you can't split a single file across two repos). `master`/`main` keeps
> shipping the simple **1.x product line**; this branch grows the advanced **2.0 Majordomo
> line**. The two can be released as **two separate products** from their respective branches
> (1.x for users who find fleet management overkill; 2.0 for multi-machine setups) — no repo
> split required for that.
>
> **To resume coding:** work on this branch, read this file + `CLAUDE.md` + the relevant
> slices of `bedrock_updater_linux.py`, then implement **Stage 1 only** (see Build stages) and
> verify before touching Stage 2. No subagents needed. The laptop's live 1.0.4 must keep
> working throughout — it only ever pulls tagged releases, never this branch tip.

## Context

Today the app manages exactly **one Server on the machine it runs on** (1.0.4-Linux, live on
the family laptop). The user's idea: grow it into a *majordomo* — one GUI that administers
**many Servers**, running **on this machine or on any machine on the home network that accepts
remote administration**, including several Servers running simultaneously on one machine
(that's how you get two Worlds live at once: one BDS process = one Active World, so "multiple
worlds running" = multiple Servers on different ports).

Decisions locked with the user (July 2026):
- **One big release** — everything lands as **2.0.0**; nothing ships until the whole vision works. The laptop stays on 1.0.4 until 2.0 is verified.
- **Home LAN only** — pairing-token + HMAC handshake; explicitly documented as not internet-facing (VPN is the answer if ever needed). No TLS of our own.
- **Sidebar fleet tree** — left panel lists Machines → Servers with live status dots; selecting a Server shows the existing 7 tabs for it.
- **Same repo, own branch** — `v2-majordomo`; `main` keeps the 1.x line; ship both as separate products if desired.

Hard constraints carried over: single file, stdlib-only (socket/ssl/json/hmac/secrets are all
stdlib), cross-platform via `sys.platform` branches, tkinter threading rule (workers marshal
via `root.after`), the GUI conventions in `docs/GUI-DESIGN.md` (confirmed-only side effects,
tabs self-refresh on open, three config systems kept distinct), and **zero regression for the
family's live setup** — config migration must be seamless.

## New terminology (extends docs/GUI-DESIGN.md)

| Term | Means |
|------|-------|
| **Machine** | A computer that hosts Servers. The local one ("This computer") always exists; remote Machines are added by pairing. |
| **Remote Administration** | The service a Machine runs to accept administrators (Settings toggle, or headless `--agent` mode). |
| **Administrator** | Any GUI instance connected to a Machine. Every install of the one file can be host, administrator, or both. |

Key sentence v2: **a Machine hosts Servers; a running Server = one World loaded on one
Bedrock Server Version.**

## Target architecture

### 1. The ServerAccess layer (the load-bearing wall)

Today the tabs mix direct file I/O (server.properties, worlds dir scans, level.dat byte scans,
allowlist/permissions.json) with direct `ServerManager` calls. 2.0 introduces **one interface,
two implementations**:

- `LocalServerAccess` — wraps today's direct code (ServerManager, BackupManager, file helpers).
- `RemoteServerAccess` — same methods, but each call becomes a JSON request over the Machine's
  connection; the host executes the *same local code* on its side.

Surface (grouped): process ops (`is_running/start/stop/restart/send_command`, output+status
callbacks, console ring-buffer replay) · files (`read/write_properties`, `list_worlds` with
size/mtime/last-run-version computed host-side, world create/rename/delete,
get/set active world, `read_gamerules`, players JSON read/write, known_players) · backups
(list/create/restore/delete, preserve items) · update (host downloads + runs its own
`perform_update`, progress streamed) · info (installed BDS version, port, platform).

The tabs talk **only** to this interface. Remote support then costs one transport, not seven
tab rewrites.

**Call pattern (keeps existing tab code shape):** ServerAccess methods are plain synchronous
calls. Quick reads stay inline exactly as today's inline file I/O (LAN round-trip is
milliseconds; a short ~2s socket timeout plus connection-state gating — calls fail fast on a
machine already marked 🔴 — bounds the worst case). Long ops (backup/restore/update/download/
stop) keep the app's existing worker-thread + `root.after` pattern unchanged. No callback
rewrite of the seven tabs.

### 2. Host service & protocol

- **JSON Lines over TCP** (stdlib `socketserver.ThreadingTCPServer`), default port **19190**,
  one connection per Machine (multiplexes all its Servers; messages carry a server id).
- Handshake: client `{"hello","proto":1,"app":"2.0.0"}` → host `{"challenge": nonce}` →
  client `{"auth": HMAC_SHA256(token, nonce)}` → host `{"ok", machine info}`. Failed auth
  throttled. `proto` field lets future versions refuse cleanly.
- Requests `{"id",op,server,params}` → `{"id",ok,...}`. Host-pushed **events** (no id):
  console lines (batched), status changes, long-op progress (backup/restore/update),
  `servers_changed`. Long ops run in host worker threads exactly like today.
- Host side runs **inside the GUI process** when the Settings toggle is on, or as the whole
  process in `--agent` mode. Either way the process that owns the BDS subprocesses serves them
  — Servers live and die with the app instance that started them (document this plainly).

### 3. Security model (honest, LAN-grade)

Pairing token (`secrets`-generated, shown as `XXXX-XXXX-XXXX` on the host with copy button;
regenerate invalidates). HMAC challenge means the token never crosses the wire; the session
itself is plaintext — fine for a home LAN, documented as such: *don't port-forward this*.
Token stored in the administrator's config (same trust level as the rest of the JSON config).

### 4. GUI: sidebar + the same 7 tabs

```
┌─ Machines ──────────┬──────────────────────────────┐
│ 🖥 This computer     │  [Server][Worlds][Players]   │
│   🟢 Family Server   │  [Configuration][Backups]    │
│   ⚪ Creative Test   │  [Update][Settings]          │
│ 🖥 Boas-Laptop       │                              │
│   🟢 Boas Familie…   │   ← existing tabs, showing   │
│ 🖥 Old-PC  🔴        │     the selected Server      │
│ [+ Server][+ Machine]│                              │
└─────────────────────┴──────────────────────────────┘
```

- `ttk.Treeview` sidebar; dots: 🟢 running · ⚪ stopped · 🔴 machine unreachable.
- Selecting a Server re-targets all tabs (the existing `<<NotebookTabChanged>>` self-refresh
  machinery does the re-scan). Configuration keeps its "never auto-reload" rule *within* a
  Server, but switching Server is a context switch: unsaved edits prompt (keep editing /
  discard & switch).
- Selecting a Machine shows a **machine page** (name, platform, app version, its Servers as
  rows with start/stop, remote-admin status, remove). Selecting the root shows the **Fleet
  overview**: every Server across Machines — status, Active World, version, players online
  (tracked from the join/leave lines we already parse) — with confirmed start/stop buttons.
  This is the majordomo payoff and replaces a separate dashboard tab.
- Confirmed-only side effects now **name the Machine**: "Stop 'Family Server' on Boas-Laptop?"
- Add Server (local): folder picker as today. Add Server (remote): admin types/pastes the
  host-side path; host validates with `is_valid_bedrock_server`. Add Machine: name, host/IP,
  port, token → test connection → save.

### 5. Config v2 & migration (grounded in the coupling inventory)

The inventory confirmed 1.0's `DEFAULT_SETTINGS` already carries **unused stubs
`server_profiles` (dict) and `active_profile`** — 2.0 makes them real:

```jsonc
{
  "config_version": 2,
  "server_profiles": { "<id>": {"name","path",           // ← was last_server_path
      "preserve_items","max_backups","compress_backups","auto_cleanup_backups",
      "auto_stop_server_before_update","auto_start_server_after_update",
      "known_players"} },                                 // ← keys leaving the flat level
  "active_profile": "<id>",                               // last-selected in the sidebar
  "machines": [ {"id","name","host","port","token"} ],
  "remote_admin": {"enabled": false, "port": 19190, "token": ""},
  // stays flat (app-level): dark_mode, window_geometry, last_zip_path,
  // check_updates_on_start, console_font_size, console_max_lines
}
```

- **Migration** on first 2.0 start: flat keys → one profile (name from the `server-name`
  property, else folder name); original config kept as `*.v1.bak`; dead keys dropped
  (`start_minimized_to_tray`, `show_notifications` — both confirmed never read).
- **Shallow-copy fix**: `load_config()` starts from `DEFAULT_SETTINGS.copy()` (shallow), so
  nested defaults like `DEFAULT_PRESERVE_ITEMS` are *shared objects* mutated in place. Profiles
  must `copy.deepcopy` their nested defaults or all Servers would share preserve/known_players
  state. Same fix in `reset_settings`.
- **Backups namespacing, rollback-safe**: new backups go to
  `<parent>/bedrock_backups/<server_dir_name>/`; the old flat `backup_*` entries are still
  *listed* (and count toward cleanup) for a profile whose parent matches — **no files are
  moved**, so rolling the laptop back to 1.0.4 still finds everything.
- **Per-server settings leave the Settings tab** for their functional homes: backup policy
  (max/compress/auto-cleanup) → **Backups tab** beside the preserve checklist it already owns;
  the two update toggles → **Update tab**. Settings keeps app-level prefs + **Remote
  Administration** + **Machines** management, and its Server Folder picker is replaced by the
  sidebar's [+ Server].

### 6. Headless agent mode

`python3 bedrock_updater_linux.py --agent [--config PATH]` — no tkinter import (guard the
import so it runs on machines without X11/tkinter), loads the same config, serves its local
Servers, logs to the app log. Lets the laptop's Servers survive GUI restarts if the user ever
wants that pattern (GUI then connects to `127.0.0.1` like any Machine). Autostart-at-boot =
docs note only (systemd/Task Scheduler), not automated in 2.0.

## Refactor surface (from the coupling inventory, file = 2477 lines today)

- The "current server" is literally `self.server_entry.get()` (a Settings-tab Entry) read at
  **~32 sites** across every tab — no accessor exists. First move: one accessor / context
  object, mechanical replacement, app still single-server and fully working.
- `ServerManager` and `BackupManager` each have **exactly one construction site**
  (`initialize_managers`, lines 1765/1769) → becomes a `{profile_id: context}` registry.
- Single things to parameterize per Server: console Text feed (→ per-server ring buffer),
  `is_updating` flag, progress bar, status labels, the trees, `preserve_vars`,
  `properties_editor` binding.
- `SERVER_EXECUTABLE`/platform constants are local-machine — remote hosts resolve their own;
  the admin never touches remote executable names.
- Persistence today is split (on_close writes geometry/paths/preserve; the Settings Save
  button writes the toggles) — v2 unifies per-profile saves.

## Build stages (internal only — one release at the end)

> **Session note:** implement one stage per fresh session, reading only this plan +
> CLAUDE.md + the relevant slices of the one source file. **No subagents needed for
> implementation** — it's a single, well-mapped file; fine-grained design (exact method
> signatures, message-by-message protocol) is settled in-line while coding Stage 2/3.

1. **Multi-Server local**: config v2 + migration; extract ServerAccess; registry of
   LocalServerAccess instances; per-Server console ring buffers; sidebar (local Machine only);
   port-collision guard before start; on_close stops all running local Servers after a
   confirm that lists them. *Checkpoint: everything 1.0.4 does, times N, locally.*
2. **Host service**: protocol + auth + host loop in-process; `--agent` mode; Settings
   "Remote Administration" section (toggle, port, token display/regenerate).
3. **Remote client**: RemoteServerAccess + Machine connection (reader thread, request/response
   correlation, reconnect with backoff, `root.after` marshaling); pairing UX; remote rows in
   the sidebar. *Checkpoint: full loopback test on the dev PC.*
4. **Fleet overview + machine page, polish, docs, packaging**: GUI-DESIGN.md new terms +
   rationale, CHANGES.md 2.0.0, CLAUDE.md, README, installer/AppImage/workflow bumps to 2.0.0.

Release: deploy to the laptop (existing staged-deploy workflow, `pre-2.0-backup/`), family
regression test, then tag `v2.0.0` → CI builds installers (only on the user's explicit go).

## Files

- `bedrock_updater_linux.py` — everything above (single file stays; likely ~3.5–4k lines after).
- `docs/GUI-DESIGN.md`, `CHANGES.md`, `CLAUDE.md`, `README.md` — conventions + docs.
- `packaging/installer.iss`, `packaging/build-appimage.sh`, `.github/workflows/release.yml` — 2.0.0.

## Risks & guardrails

- **Family server continuity**: laptop untouched until final deploy; migration rehearsed first
  against a *copy* of the laptop's real config; `config.json.v1.bak` safety net.
- **Whole-file refactor risk**: Stage 1 extracts ServerAccess with *identical behavior* and is
  verified before any multi/remote code lands on top.
- **Two administrators at once**: host serializes ops; console events broadcast; properties =
  last-write-wins (same as two local edits today) — family-scale, documented.
- **Windows firewall** prompts on first listen — document.
- **Update pipeline stays destructive** — remote confirmations name Machine + Server; preserve
  list honored host-side exactly as today.

## Verification

1. `python -m py_compile` after every stage; GUI smoke-run on Windows.
2. **Multi-local**: two scratch BDS installs on the dev PC, both running at once (distinct
   ports), independent consoles/players/gamerules; port-collision guard fires; close-with-
   running-servers confirm.
3. **Loopback remote**: enable Remote Administration locally; second GUI instance with a
   scratch `--config` adds Machine `127.0.0.1` → drive all 7 tabs remotely; kill the host →
   red dot + auto-reconnect; wrong token → clean refusal.
4. **Agent mode**: `--agent` headless on the dev PC, GUI administers it.
5. **Migration rehearsal**: copy the laptop's real config over ssh, run 2.0 against it, verify
   the profile (path, preserve items, known_players) survived intact.
6. **Real network test**: laptop hosts (staged per the deploy workflow, family idle), dev PC
   administers: console, world-switch confirm flow, backup, dry-run update.
7. 1.0.4 feature regression checklist on the migrated profile.

## Out of scope for 2.0 (explicitly)

Internet exposure/TLS · cross-machine backup copies · uploading ZIPs to hosts (hosts download
themselves) · autostart-at-boot automation · splitting the single file.
