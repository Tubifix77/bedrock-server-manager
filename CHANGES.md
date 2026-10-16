# Changelog

All notable changes to this project will be documented here.

---

## [Unreleased] — next release: 3.1

### Added
- **Checkboxes for hidden settings in 📝 Configuration.** Three settings Bedrock understands, but
  that its stock `server.properties` doesn't list (or only lists as a comment), are now checkboxes
  at the top of the page. A setting left as it was isn't written, so the file stays as Mojang
  ships it. Each takes effect on the next start.
  - **Send server telemetry to Mojang** (`emit-server-telemetry`, off by default). Bedrock prints
    its "Telemetry is currently not enabled" message at every start while it's off;
    `=false` doesn't silence it.
  - **Turn off Vibrant Visuals for players** (`disable-client-vibrant-visuals`, on by default per
    Microsoft's reference). Untick it to let players' devices use Vibrant Visuals.
  - **Save players' chat to a log file (experimental)** (`chat-logging-enabled`, off by default):
    `chat_logs/ChatLog_<date>.txt`. Mojang documents it only for Minecraft Education servers; the
    vanilla engine has the same code, but it hasn't been verified with real player chat yet.

- **Every on/off setting in 📝 Configuration is now a checkbox** instead of a text field you had
  to type `true` or `false` into — 18 of them in Mojang's stock file (allow cheats, online mode,
  allowlist, LAN visibility, …), plus any other setting whose value is `true`/`false`. Free-text
  settings that could hold those words (the Server's name, the seed) always stay text fields.
  Rows keep their order from the file, and saving without changes leaves the file untouched.
- **The sidebar, 🌐 Fleet and Machine pages name Servers like the top line**: `Server N: <Active
  World>`, numbered per Machine, instead of an old list name such as "Dedicated Server" — also
  for Servers on another Machine (whose host now reports each Server's Active World; a host on an
  older version shows its list name as before). Start/Stop confirmations use the same names, and
  adding, removing or renaming a Server shows up on connected Administrators right away.
- **"Show in Minecraft's LAN list" in the + window**, ticked by default for every new Server
  (before, only the first Server was LAN-visible). Only one Server per computer can be listed at
  a time — the one started first — so with it ticked, whichever Server runs alone shows up in
  the LAN list; the others are still reached by IP and port.
- **✏ Rename a Server**, next to + and − on the top line. One name for the selected Server: its
  World (folder and Active World, so the top line changes), its name in the sidebar and 🌐 Fleet,
  and — ticked by default while players still see Mojang's stock "Dedicated Server" or the old
  name — the name players see in their server list. Handy after copying a World to a new Server.
  The Server must be stopped (its World folder is renamed); a name another World already has is
  refused, and a change of capitals only works too.
- **📍 Teleport on the 🎮 Server tab**, under the console's Quick buttons: pick a player, pick who
  to move them to, press **Go**. It uses the console's `tp` command, so it works with cheats
  off and needs no Operator role. The pickers list who's online (or every known player when
  that isn't known), and the Server's answer shows next to the button: **✓ … was teleported
  to …**, or **✗ … isn't online, so nobody was moved**.
- **The game-mode buttons on 👥 Players now answer right there.** Under ⛏️ Survival /
  🧱 Creative / 🗺️ Adventure, one line shows the Server's actual reply, instead of it going only
  to the console on another tab: **✓ LivBoas17 is now in Creative** (green), **✗ LivBoas17 isn't
  online…** (red), or that the Server didn't answer. It fades after 20 seconds, is cleared when
  you switch Server, and works the same for remote Servers.

### Fixed
- **A remote Machine didn't hear about Servers started on the Machine itself.** A host only
  reported a Server's starts and stops once an Administrator had used that Server, so a Server
  started at the host stayed "Stopped" on the other computer. Every Server is reported now.
- **Stop Selected did nothing for a remote Server** on the 🌐 Fleet and Machine pages: the row
  assumed the Server was stopped, so the button quietly gave up. It now knows the Machine's last
  reported state.
- **Opening 🌐 Fleet or a Machine page left the top line on the previous Server** (still
  highlighted, with ✏ usable). The top line now redraws: no Server highlighted on Fleet, and a
  remote Machine's page shows that Machine's Servers.
- **Yellow hover tips could get stuck on screen**, most often over the Server 1 / Server 2
  buttons. The top line rebuilds its buttons whenever a Server starts or stops; a tip showing at
  that moment lost both its "mouse left" event and its 2-second timer, and repeated hovering
  could pile several up. Now only one tip exists at a time, its timer belongs to the main
  window, and it closes when its button is clicked or rebuilt.

---

## [3.0] "Servers" — 2026-10-11

Several completely separate Servers on one Machine (say, survival for the grown-ups and
creative for the kids), each with its own Worlds, settings, players and backups, and updating
without hunting for ZIPs. Design: [docs/V3-SERVERS-PLAN.md](docs/V3-SERVERS-PLAN.md).

### Added
- **The Server strip** on the top line: `Server 1: <Active World>`, `Server 2: …`, with a green
  dot for a running Server. Numbers are display-only and close up when a Server is removed.
  When the line runs out of room, the rest go into **▾ N more**. The selected Server always stays
  visible.
- **＋ New Server**: name, game mode, a new World (with an optional **seed**) or a copy of an
  existing one. The app downloads and installs the newest Bedrock Server Version itself. The first
  Server gets port 19132 and shows up in Minecraft's LAN list; later ones get the next free port
  (19134, 19136, …) and are added by IP and port, because only one Server per computer can be
  LAN-visible. The allowlist starts off, so players can join right away.
- **− Remove a Server**: pick any Server (nothing preselected; Servers with players online are
  greyed out), see exactly what will be deleted (folder, Worlds, its own backups), then type the
  World's name to unlock **Delete permanently**. A list entry pointing at something that isn't a
  Bedrock server folder is only taken off the list, and nothing on disk is deleted.
- **World seed.** Both ＋ New Server and Worlds ▸ Create take an optional seed. It's always
  written to `level-seed`, emptied when none is given, so a seed meant for one World is never
  silently reused for the next.
- **One-button Update.** The app asks Mojang for the newest release itself:
  `Installed: X · Latest: Y` and **⬆ Update to Y** (or **✓ Up to date**). A specific ZIP or a typed
  version is still available under **Advanced**, and Advanced opens by itself if Mojang can't be
  reached.
- **Update all Servers** (with two or more Servers): one download and one confirmation listing
  every Server. A running Server is stopped before its turn and started again afterwards. A Server
  that fails is reported and doesn't stop the others. If it failed before anything was changed,
  it's started again too.
- The installed Bedrock Server Version is recorded (`.bsm_bedrock_version` in the Server's folder,
  also read from the engine's own startup line), so it's no longer shown as "Unknown".
- Who is online is tracked per Server from the console's join/leave lines.

### Changed
- The Advanced ZIP field is no longer remembered between sessions, because a remembered old ZIP
  would take over the Update button.
- A Server can't be started (locally or by a remote Administrator) while its files are being
  updated.
- First-run hints now point to **+** instead of "set the Server Folder in ⚙️ Settings", and the
  sidebar's ➕ Server button is gone (+ on the top line replaces it; adopting an existing folder
  is in the + window).

### Fixed
- **Update on Windows could fail under a long install folder**: Bedrock's resource packs nest
  deep enough to pass the 260-character path limit. Wipe, extract and restore now use long paths,
  as backups already did.
- A few error messages shown after a failure could themselves crash (`e` was gone by the time the
  message ran): Add Machine, Start and Restart.
- **Settings can no longer be lost to a crash or power cut.** The settings file was overwritten
  in place, so an interrupted save could leave it half-written. On the next start the app would
  then silently fall back to defaults and overwrite it, losing every Server profile, paired Machine
  and known player. Saves now go through a temp file that is swapped in atomically. If the file is
  ever unreadable anyway, it is kept as `…config.json.corrupt-<timestamp>` and the app says so at
  startup instead of quietly starting empty.
- **The log recorded every message as INFO**, errors included, because the main window had two
  `log()` methods and the one that won ignored the level. Errors and warnings are now logged as
  such.
- **Crashes inside the app are now written to the session log** with a traceback. Before, an
  unhandled error in a button handler or background thread went only to the terminal, so it was
  lost whenever the app was started from its icon.
- A few failures that were swallowed without a trace are now logged (with no change in behavior):
  the Server's console reader stopping, an old backup that couldn't be deleted, and remote-update
  handlers failing. A broken console callback is logged once, not once per console line.

---

## [2.0.1] "NetherNet" — 2026-09-26

Keeps the manager working with **Bedrock Server Version 1.26.5x**, where Mojang made NetherNet
the only transport players can connect over — plus fixes from the first real-world LAN use of
2.0.

### Fixed
- **Players can't connect after updating to 1.26.5x ("TRANSPORT TYPE ERROR … not set to
  NetherNet").** Update keeps your `server.properties`, which meant any setting Mojang added in a
  new version was silently lost — here `transport`, whose stock value in 1.26.5x is `nethernet`.
  Without it BDS falls back to RakNet and current clients get a transport error. **Update now
  merges in every setting the new version's stock `server.properties` has that yours lacks**
  (with Mojang's comment block, and each one named in the Activity Log). Your existing values
  always win, obsolete keys are left alone, and this covers any future setting Mojang adds, not
  just `transport`.
- **Already updated with an older manager?** When the console shows that NetherNet error and
  `transport` is missing, the app offers — after a yes/no — to add `transport=nethernet` (works
  for local and remote Servers); press **Restart** to apply. A deliberate `transport=raknet` is
  respected. Note NetherNet listens on **TCP** `server-port` (19132) as well as UDP, so a firewall
  must allow TCP 19132 too.
- **A running remote Server showed as "Stopped"** in the administrator until you pressed Start
  locally. The host only pushes status-change events on start/stop, so a Server that had been
  running for hours never announced itself to a late-joining admin. The running state is now
  seeded from the Machine's reported server list on selection and re-confirmed from the
  authoritative `get_info().running` — correct for local and remote alike.
- **Sidebar heading** over the Machines tree said "This computer"; it now reads "Machines"
  (the tree lists every Machine — the "This computer" node inside it is a separate thing).

### Added
- **Single window: launching the app again raises the one that's already open.** Opening it a
  second time (e.g. the desktop shortcut while one's already running from the taskbar) used to
  start a second GUI, and the two fought over the one config file and the one running Server,
  stranding the newer session. The GUI is now single-instance (a loopback-only lock on
  `127.0.0.1:49732`); a second launch brings the running window to the front and exits. If
  something unrelated holds that port, the app still launches normally rather than refusing to
  open. Verified on **both Windows and Linux**, with the Server stopped and running.

---

## [2.0.0] "Majordomo" — 2026-07-15

The app grows from managing one Server to managing a fleet — several Servers on this
machine, or on any machine on the home network that accepts remote administration, all from
one GUI. Full design rationale in [`docs/GUI-DESIGN.md`](docs/GUI-DESIGN.md) and the build
history in `docs/V2-MAJORDOMO-PLAN.md`. See [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md) for a
plain walkthrough of everyday use.

### Added
- **Multi-Server, one app**: add as many local Servers as you like (sidebar's ➕ Server, each
  with its own settings/backups/known players); several can run **simultaneously** — a
  port-collision guard refuses to start two on the same port, and closing the app with several
  running lists them all by name in one confirm before stopping them.
- **Sidebar** (Machines → Servers), **collapsed by default** so a single-Server install looks
  just like plain 1.0.4: a small "▶ Machines" button (always visible, top-left) expands it into
  a resizable pane showing every configured Server, local and remote, with a running/stopped
  dot; selecting one points the existing 7 tabs at it without disturbing anything else running
  in the background. "◀ Hide" collapses it again; the choice is remembered across restarts.
- **🌐 Fleet overview**: the sidebar's root shows every Server across every Machine in one
  table — double-click to open one, or Start/Stop Selected without leaving the page.
- **Remote administration over the LAN**: any install can host (Settings ▸ Remote
  Administration: enable, port, pairing token with copy/regenerate) or run headless
  (`--agent`), and any install can administer another one (sidebar's ➕ Machine: name,
  host/IP, port, token, Test connection). A paired remote Server is driven from **every tab**
  exactly like a local one — status, live console, start/stop/restart, commands, worlds
  (including rename/delete), players, configuration (reads and saves), and backups. Security
  is honest LAN-grade: an HMAC-authenticated pairing handshake, plaintext session after that —
  documented as not internet-facing; use a VPN (Tailscale/WireGuard) to reach a Machine
  off-LAN rather than port-forwarding.
- **Machine page**: selecting a Machine in the sidebar shows its name/platform/version,
  connection status, and its own Servers; remote Machines get a Remove Machine button.
- Backups are now **namespaced per-Server** on disk so Servers sharing a parent folder never
  mix backups — nothing existing gets moved, so a rollback to 1.0.4 still finds every backup
  where it was.
- Per-Server settings (backup policy, update toggles) moved off the app-level Settings tab
  onto the Backups and Update tabs, where the data they configure actually lives.

### Changed
- Config is now schema **v2** (`server_profiles` + `active_profile` + `machines` +
  `remote_admin`); an old 1.x config is migrated automatically on first run, with the original
  kept as `*.v1.bak`.
- Settings tab is now app-level only (interface prefs, Remote Administration, About); the
  Server Folder picker there now only *relocates* the currently selected Server — adding one
  is the sidebar's ➕ Server.
- Default window size widened from 900×700 to **1200×700** — some tabs clipped buttons at the
  old width.

### Fixed
- **GUI freeze opening the Server/Worlds/Update tabs** against a large, actively-played world:
  three places (`update_server_info`, `refresh_worlds`, `refresh_world_combo`) computed World
  sizes synchronously on the UI thread on every tab visit; that disk walk is slow when the real
  engine is concurrently writing to the same World, and got dramatically worse than in testing
  (which only ever used small, freshly-generated Worlds). Now backgrounded like every other
  slow operation in the app.

### Deliberately out of scope
- **Update stays local-only.** It wipes and replaces the entire Server install; running
  something that destructive blind over a network link — where a dropped connection mid-copy
  could leave a Server half-wiped with no one there to notice — isn't a risk worth taking.
  Everything else works local or remote identically.
- Internet exposure/TLS, cross-machine backup copies, uploading ZIPs to a host (a host
  downloads its own updates), and autostart-at-boot automation are not part of 2.0.

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
  - Player names + XUIDs are **learned automatically** from join/leave console lines (tolerant
    of wording differences between Bedrock Server Versions), stored as `known_players` in the
    app config. A **🔍 Scan console for players** button re-reads the whole console buffer in
    case a line was missed, and learned XUIDs are **backfilled** into allowlist entries that
    were added by name only.
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
