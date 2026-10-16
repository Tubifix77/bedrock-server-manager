# GUI design & terminology (as of 3.0 "Servers")

The naming convention and tab layout agreed for this app. Use these words consistently —
in the GUI, in docs, and when discussing changes. The point is to undo Bedrock's own
server/world mix-up (`server.properties` holds both `server-name` and `level-name`).

## Terminology

| Term | Means | Notes |
|------|-------|-------|
| **Bedrock Server Version** | The engine build (`bedrock_server` + libs + stock packs). | Mojang's own term. It's what **Update** swaps. Never call this "the server". |
| **Server** | One configured install: name + port + config + its Worlds. | A Machine can have several, and several can run at once (different ports). Shown as **Server 1, Server 2, …** — a display-only number (its position), not a name stored anywhere. Created by **+** into a hidden permanent folder `server-<id>`. The path field stays **"Server Folder"**. |
| **World** | A save under `worlds/<name>/`, belonging to a Server. | A Server has many; switching never touches the others. |
| **Active World** | The one World the Server loads — the `level-name` pointer. | Set via the dropdown on the Server tab. Changing it takes effect on next start. |
| **backup** | A copy of a Server's Worlds + config (the preserve list). | Kept lowercase/unchanged — Mojang doesn't use "snapshot", and in Minecraft "snapshot" means Java dev builds. Backups are **per Server**, contain **all** Worlds, and restore **wholesale**; namespaced per-Server on disk since 2.0 so Servers sharing a parent folder can't mix backups. |
| **Machine** | A computer that hosts Servers. | The local one is always "🖥 This computer"; others are added via ➕ Machine (host/IP + port + pairing token). One app install = one Machine, whether it's only administering, only being administered, or both. |
| **Remote Administration** | The service a Machine runs so another Machine's administrator can control it over the LAN. | Toggled in ⚙️ Settings (in-process) or run headless as `--agent`. LAN-only by design — see Security below. |
| **Administrator** | Whichever GUI (or `--agent`) instance is *connected to* a Machine to control it. | Every install can be a host, an administrator, or both at once. |
| **Fleet** | Every Server across every Machine, in one place. | The sidebar's "🌐 Fleet (All Servers)" root — see/start/stop anything from one screen. |

Key sentence: **a Machine hosts Servers; a running Server = one World loaded on one
Bedrock Server Version.**

## Security model (LAN-grade, honestly)

Remote Administration uses a pairing token (shown as `XXXX-XXXX-XXXX`, regenerable) and an
HMAC challenge/response handshake — the token itself never crosses the wire. The session
after that is plaintext. This is **deliberately home-LAN-grade, not internet-grade**: don't
port-forward it. If you need to reach a Machine off your LAN, put it behind a VPN (e.g.
Tailscale/WireGuard) rather than exposing the port directly — the Settings panel says so.

**Per-World version:** each World's `level.dat` carries `lastOpenedWithVersion` (NBT) — the
save format ratchets up to whatever version last ran it, and **won't load on older versions**.
Shown as "Last Run On" in the Worlds list and on the Active World line in Active Server
Information (with ⚠ when a World is newer than the installed Bedrock Server Version).

## The top line: Server strip, + and − (3.0)

```
▶ Machines   [🟢 Server 1: Boas Familie Server] [⚪ Server 2: Kids Creative] [▾ 2 more]   [✏] [+] [−]
```

- **One button per Server** of the Machine in view: `Server N: <Active World>` (World name cut
  to 22 characters + `…`, full name and players online in the tooltip), with a 🟢/⚪ running dot.
  Clicking one selects that Server, exactly like the sidebar.
- **Numbers are positions, not names.** Remove Server 2 and Server 3 *becomes* Server 2 — the
  user insisted ("people like order"), and it's safe because nothing on disk carries the
  number (folders are `server-<id>`, backups follow the folder).
- **Overflow**: as many buttons as fit; the rest go into **▾ N more**. The selected Server always
  stays visible (it takes the last visible slot). No wrapping to a second line.
- **+ New Server**: name players see, game mode, then *A new World* (optional **seed**) or *A copy
  of an existing World* (refused while that World's Server runs, so the copy is complete). The
  newest Bedrock Server Version is downloaded and installed automatically — the user never picks
  a ZIP. The first Server on a Machine gets port 19132; later ones the next free port pair
  (19134/19135, 19136/…). **☑ Show in Minecraft's LAN list** (`enable-lan-visibility`) is ticked
  by default for every new Server: only the first-*started* Server gets UDP 7551 (LAN discovery)
  — identical on Windows and Linux — so ticking it on several is harmless (the others just
  aren't listed), and it means whichever Server is running alone shows up in the LAN list. The
  confirmation tells players the address + port to add by hand whenever it may not be listed. `allow-list=false` (stock
  1.26.5x ships it on with an empty list). *Use an existing Bedrock folder instead…* covers
  adopting an install made outside the app.
- **✏ Rename** (between the strip and +): one dialog renames the selected local Server's Active
  World (folder + `level-name`, i.e. what the strip shows), its list name (sidebar/Fleet) and,
  optionally, `server-name` — that box starts ticked only while players still see the stock
  "Dedicated Server" or the old name, so a name chosen on purpose is left alone. Greyed out with
  the reason while the Server runs (its World folder moves).
- **− Remove a Server**: first a picker of *every* local Server, nothing preselected, Servers
  with players online greyed out (`1 player online`); then a page listing exactly what goes
  (folder, Worlds, its own backups — never an older shared backup folder), a warning if it's
  running (it's stopped first), and **Delete permanently** unlocked only by typing the Active
  World's name exactly. Refuses anything that isn't a Bedrock server folder (needs the engine
  binary *and* `server.properties`, never a drive root or home) — such an entry is only taken
  off the list. Disabled with a single Server.

## Sidebar (Machines → Servers, plus Fleet)

A resizable left pane, replacing the old single-Server assumption:

```
🌐 Fleet (All Servers)
🖥 This computer
   🟢 Server 1: Boas Familie Server   ← local Servers, labelled as on the top line
   ⚪ Server 2: Kids Creative
🖥 Boas-Laptop
   🟢 Server 1: Boas Familie Server   ← remote Server, same tabs as local
🖥🔴 Old-PC                 ← configured but unreachable right now
[➕ Machine]
```

- **Collapsible, collapsed by default** (`sidebar_collapsed` in config, defaults `True`): a
  single-Server user never has to look at Machines/Fleet at all — the app opens looking just
  like 1.0.4. A small toggle button (**▶ Machines** / **◀ Hide**) sits in a slim bar above the
  pane, outside the `ttk.PanedWindow` itself, so it's always reachable even while the sidebar
  is hidden — it's the only way back once collapsed. Toggling calls
  `main_pane.forget()`/`main_pane.insert(0, ...)` on the sidebar frame and persists the choice
  immediately (`save_config`), independent of the Server currently selected (hiding the sidebar
  never stops or deselects anything — `self.active_access` doesn't care whether the tree
  widget is currently packed into the pane).
- **Labels** (3.1): every Server is `Server N: <Active World>` in the sidebar, the Fleet overview,
  the Machine pages and their confirmations, exactly as on the top line, numbered per Machine
  (`_machine_server_entries()`). A host reports each Server's Active World (`world`); an older
  host doesn't, and its list name is shown instead. Adding, removing or renaming a Server on a
  host is pushed to connected Administrators at once (`notify_servers_changed`).
- **Selecting a Server** (local or remote) points the 7 tabs at it — same tabs, same controls,
  whether the Server is on this computer or across the LAN. A running/stopped dot follows it
  in the sidebar; switching away never stops it.
- **Selecting a Machine node** shows a **Machine page**: name, platform, app version, connection
  status (remote), and its own Servers with Start/Stop — plus 🔌 Remove Machine for remote ones.
- **Selecting 🌐 Fleet** shows every Server across every Machine in one table — double-click a
  row to open it, or Start/Stop Selected without leaving the overview. This is the sidebar's
  root and the majordomo payoff: one screen to see the whole household.
- **➕ Machine** opens Add Machine (name, host/IP, port, pairing token, Test connection).
  Adding a Server moved to the top line's **+** in 3.0 (Settings ▸ Browse still only relocates
  the *currently selected* Server's folder).

## Tab layout (order = daily use first)

| # | Tab | Role | Holds |
|---|-----|------|-------|
| 1 | **🎮 Server** *(home)* | See & run the Server | **Active Server Information** (name, Bedrock Server Version, Active World + its last-run version, gamemode, port, worlds) · Start/Stop/Restart · **🎲 Gamerules** dialog (reads the Active World's `level.dat`, applies live via `gamerule` commands — gamerules are per-World and are *not* `server.properties`) · **Active World dropdown** (existing Worlds only, plain names — the selected entry *is* the active World, no extra "(active)" tag; greys out while running with an inline "Not available until the running Server is stopped" hint — no static helper text while stopped) · network info · console (half height) with Quick buttons and **📍 Teleport** (two player pickers + Go; the console's `tp`, so cheats can stay off; the Server's answer is shown beside it, as for the game-mode buttons on Players) — all of it works identically for a remote Server |
| 2 | **🌍 Worlds** | Create & manage Worlds | **✨ Create New World** (hero button — one small dialog: name + optional **seed**; ends by jumping to Active Server Configuration) · Worlds list with sizes, dates, **Last Run On** — the Active World is marked **✅ ACTIVE**; a created-but-not-yet-generated Active World shows as an orange *"created on next start"* pending row · **Set as Active World** (if the Server runs: confirm → stop nicely → switch → start again) · Rename / Delete (refuse a running Server; Delete refuses the Active World) — remote-capable |
| 3 | **👥 Players** | Manage people | (3.1: the game-mode buttons show the Server's answer underneath — ✓ now in X / ✗ isn't online / no answer — fading after 20 s.) **Access**: allowlist editor + "restrict joining" toggle (writes `allow-list`, live `allowlist on/off`; empty-list warning; Bedrock has **no blacklist**) · **Roles**: `permissions.json` visitor/member/operator (live `permission reload`) · **Game mode per player** (live `gamemode` command → mixed modes; warns if `force-gamemode=true`). Names+XUIDs auto-learned from join lines into `known_players` (tolerant regex — wording varies by version), with a **🔍 Scan console** button that re-reads the whole buffer and backfills missing allowlist XUIDs. Remote-capable. |
| 4 | **📝 Configuration** | Edit the Server | Page header: **Active Server Configuration**. `server.properties` editor (skips `level-name` — that's the Active World): every setting whose value is `true`/`false` is a checkbox (except free-text keys in `FREE_TEXT_KEYS` — `server-name`, `level-seed`, …), the rest are text fields, in file order. Settings Bedrock understands but its stock file omits are offered as checkboxes at the top (`OPTIONAL_CHECKBOXES`: `emit-server-telemetry`, `disable-client-vibrant-visuals`, and `chat-logging-enabled` marked *experimental*); one that isn't in the file and is left at Bedrock's default is never written. Natural stop between creating a World and first start (gamemode, difficulty…). Remote-capable — a save round-trips to the host and back. |
| 5 | **💾 Backups** | Per-Server backups | Header **names the Server** and where backups live · **Backup Settings** (max kept / compress / auto-cleanup — moved here in 2.0, it's per-Server) · **What to back up** checklist (the preserve list — same list updates preserve) · backup list (~⅓ height) · restore/delete. Remote-capable. |
| 6 | **🔄 Update** | Swap the Bedrock Server Version | `Installed: X · Latest: Y` (asked from Mojang, cached 10 min, 🔄 Check again) · one button: **⬆ Update to Y** / **⬇ Install Y** / **✓ Up to date** · **⬆ Update all Servers (N)** with two or more Servers · **Advanced ▸** (a specific ZIP, or *Enter a version…*; opens by itself if Mojang can't be reached) · Dry Run / Open Folder (small, right) · **Update Settings** (auto-stop/auto-start) · activity log. **Local-only, deliberately** — see Rationale. |
| 7 | **⚙️ Settings** | App-level | **Current Server's Folder** (relocate only — adding a Server is **+** on the top line) · **Remote Administration** (enable toggle, port, pairing token + copy/regenerate) · interface prefs · dark mode |

## Rationale (decisions, so they don't get relitigated)

- **Server is home**: status + control + "what am I running" belong on the landing tab.
  Update is a maintenance tool, not the home screen.
- **The Server Folder is configuration**, not a daily control → Settings. First-run guidance:
  info panel and log point to ⚙️ Settings; fresh installs point to 🌍 Worlds.
- **Dropdown over free text** for the Active World: only real folders are offered, so a typo
  can't silently create an empty world. Creating (which legitimately needs free text) is an
  explicit, separate flow on Worlds.
- **"Active Server ___" prefix pairs the two panels**: *Active Server Information* (read) on
  Server, *Active Server Configuration* (edit) as the config page's header. The tab itself is
  just **Configuration** to keep the tab bar compact (user feedback, July 2026) — the full
  paired name greets you on the page. "Active" refers to Worlds only otherwise.
- **The preserve list lives with Backups** because "what a backup contains" and "what an
  update preserves" are the same list — Update reads it from there.
- **"Worlds", not "New"**: in a *server manager*, a tab called "New" reads as "new Server" —
  which is a different (future) feature.
- **Three config systems, told apart honestly** (v1.0.4): `server.properties` (Configuration
  tab) vs **gamerules** (per-World, `level.dat`, set via `gamerule` command → the 🎲 dialog)
  vs **player files** (`allowlist.json` / `permissions.json` → the Players tab). Don't blur
  them — "1 person sleeps" is a gamerule, not a property, and Bedrock has **no blacklist**
  (exclusion = allowlist enforcement without that player). Live changes go through console
  commands (`allowlist on/off/add/remove`, `permission reload`, `gamemode`, `gamerule`) —
  the console is never blocked by `allow-cheats=false`.
- **No *silent* auto-stop side effects** (user decisions, July 2026): ambient controls (the
  Active World dropdown) grey out with an inline reason while the Server runs — they never
  stop it for you. The one exception is the explicit **Set as Active World** button, which
  *may* stop → switch → start again, but only after a confirmation dialog. Rule of thumb:
  a click may restart the Server only when the user has just said yes to exactly that.
  Update follows the same rule: its confirmation states the running Server will be stopped
  nicely first (and restarted after, if that setting is on), and the stop runs in the
  update's worker thread so the UI never freezes.
  Corollary: prefer state-driven hints that appear only when relevant over permanent
  helper sentences.
- **Tabs self-refresh on open** (user feedback, July 2026): dynamic views (Worlds list,
  Active Server Information) re-scan when their tab is selected — the user should never need
  a Refresh button to see reality. Exceptions: Active Server Configuration never auto-reloads
  (unsaved edits must survive tab hopping) and Backups stays manual (sizing every backup
  walks thousands of files).
- **server-name is not the World name — and no hard tie**: `server-name` is what players see
  in their in-game server list; the Active World is which save is loaded. Tying them would
  re-conflate what this design separates. Instead: a gray hint next to `server-name` in the
  editor, and when creating a World while the Server still has the stock name
  ("Dedicated Server"), a one-time offer to name the Server after the new World.
- **One access object, every tab** (2.0): each tab talks to `self.active_access` — a local
  `ServerService` or a `RemoteServerAccess` — never to `ServerManager`/file-paths directly.
  That's *why* the tabs are remote-capable at all: the same code path serves both, so there's
  no separate "remote UI" to keep in sync.
- **Confirmed-only side effects now name the Machine too**: "Stop 'Family Server' on
  Boas-Laptop?" — the existing confirm-before-restart rule (see below) just gained a location.
- **A still-running Server you navigate away from keeps running** — selecting a different
  Server (local or remote), the Fleet view, or a Machine page never stops anything. That's the
  whole point of a majordomo: several Servers running while you look at one, or none.
- **Update stays local-only, on purpose** (2.0): it wipes and replaces the entire Server
  install. Running something that destructive blind over a network link — where a dropped
  connection mid-copy could leave a Server half-wiped with no one there to notice — isn't a
  risk worth taking. Everything else (status, console, start/stop, commands, worlds including
  rename/delete, players, configuration, backups) works identically local or remote.
- **Opening a folder, or backup cleanup, stays local too** — they're filesystem actions on
  whichever machine you're sitting at; there's no "open Explorer on a computer across the LAN".
- **Update needs no homework** (3.0): the app asks Mojang's download-links endpoint for the
  current release (never a preview) instead of making the user look a version up on the Wiki,
  type it, and pick a ZIP. The endpoint is undocumented, so the old manual way stays under
  Advanced as the fallback. The installed version is recorded by the app itself
  (`.bsm_bedrock_version` in the Server's folder, also refreshed from the engine's own
  `Version:` startup line) because Mojang's files no longer carry it.
- **Update all Servers** (3.0): one download, one confirmation listing every Server (⬆ to do /
  ✓ already current / – skipped and why), then one Server at a time. A running Server is stopped
  before its turn and started again after; a failure is reported and the others carry on — and
  if it failed before anything was changed, it's started again too. A Server whose *What to back
  up* lacks `worlds` is skipped (update it on its own, where that choice is confirmed). While a
  Server's files are being swapped it can't be started, locally or by a remote Administrator.
- **The seed is asked where a World is made** (3.0) — + and Worlds ▸ Create — and always
  written to `level-seed`, emptied when none is given. `level-seed` is only read when Bedrock
  generates a World but stays in `server.properties` afterwards, so leaving an old value would
  silently give the next new World the same landscape.
