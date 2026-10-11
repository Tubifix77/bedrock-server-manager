# 3.0 "Servers" — design note

> **STATUS: BUILT (2026-10-11)** — every build step below is done and verified on Windows and
> Linux (scripted tests against real engines, plus a replica of the homelab's real 2.0.1 config),
> and it runs on the homelab. Differences from the draft: the installed version lives in a marker
> file, not a config key (see *Config changes*), and the World **seed** was added to + and
> Worlds ▸ Create on the user's request.
> **Version:** 3.0 rather than 2.1 — 2.0 could technically run several Servers per machine,
> but this is the first release where users can actually do it, a user-facing step as big as
> 1 → 2 (more Machines) was. Decided with the user 2026-10-11.

## Why

Since 2.0 the app can run several Servers on one machine at once (each in its own folder, with
its own Worlds, players, configuration and backups), but almost nobody would discover it:

- Adding a Server means unzipping Bedrock into a new folder **by hand**, picking a free port, then
  ➕ Server in a sidebar that is hidden by default.
- Updating needs the user to read the newest version off the Minecraft Wiki and **type** it in
  (the two `HTTP 404` lines in the homelab logs are typos from that step).
- Three different names collide: the manager's entry, `server-name` (what players see) and the
  Active World. Both shipped defaults read "Dedicated Server".

The motivating use case: the kids play **creative**, the parents play **survival**, and each
should be its own Server. Measured on the homelab (i3-3110M, 2 cores, 7.6 GB): one Server with
one player uses ~0.6–0.7 of a CPU thread and ~1 GB RAM, with the machine ~78% idle. Two Servers
are comfortable; three are realistic with light use.

## Goals

1. Make several Servers on one machine **obvious and easy**: create, switch, remove.
2. **No file hunting, no typing version numbers.** New Servers and updates fetch the newest
   Bedrock Server Version automatically.
3. Keep the single-Server look for people who only ever have one.

Non-goals: the Server model itself doesn't change (one Server = one folder = one running World,
because that's how Mojang's server works). There's no sharing of files between Servers, no new
remote abilities (creating/removing Servers stays local, like Update), and no changes to how
Worlds, Players, Configuration or Backups work inside a Server.

## 1. The top line: a Server strip

```
▶ Machines  [🟢 Server 1: Boas Familie Server] [⚪ Server 2: Kids Creative] [▾ 3 more]   [+] [−]
```

- **Labels are "Server N: \<Active World\>".** The number is **display-only**: it is the Server's
  position in the list (creation order), worked out each time the strip draws, and never stored
  or written into anything Minecraft sees. Removing Server 2 makes the old Server 3 show as
  "Server 2". When a Server's Active World changes, its label follows automatically.
- Each button shows 🟢 running / ⚪ stopped. The selected Server's button is highlighted, and
  the seven tabs below always belong to it.
- **Overflow:** "▶ Machines" stays pinned left and "+ / −" pinned right. Long World names are
  shortened with "…" (full name on hover). Servers that still don't fit go into a "▾ N more" list
  with the same running dots. The selected Server is always one of the visible buttons. Nothing
  wraps onto a second row, and the window never grows by itself.
- **Scope:** the strip shows the Servers of the Machine picked in the Machines sidebar (this
  computer by default). For a remote Machine, + and − are greyed out with an inline reason
  ("Create and remove Servers on the Machine itself"), matching how Update is local-only.
- With a single Server the strip is just its button plus **+**, so the 1.0.4-style look stays.
- The sidebar keeps Machines and 🌐 Fleet; its local "➕ Server" button is replaced by **+**.

## 2. "+" — create a new Server

A short dialog, then everything is automatic:

1. **Ask:** a name players will see (prefilled, e.g. "Kids Creative"), gamemode (survival /
   creative / adventure), and the start World: **new World** (default) or **copy of an existing
   World** from another Server.
2. **Fetch the newest version** from Mojang (§4) and **download it with a progress bar**. If that
   exact ZIP is already in the Downloads folder, reuse it.
3. **Install** into a new, completely separate folder next to Server 1's (e.g.
   `/home/boas/Bedrock/server-3f9a21c0` on Linux, `D:\Games\server-3f9a21c0` on Windows). Its
   hidden folder name is permanent and never shown, so renumbering never renames anything on disk
   and backups (filed per folder name) can never mix.
4. **Configure:** set `server-name` to the name given, `gamemode`, the new World, and a **free
   port** (the first one from 19134 upward that no other Server uses and nothing on the machine
   is listening on). LAN visibility is set according to the NetherNet test in §7.
5. **Open the new Server's Configuration tab**, not started, so the user can review before the
   first start.

A small secondary link in the dialog, "Use an existing Bedrock folder instead", keeps today's
import path for people who already have one.

## 3. "−" — remove a Server

Pressing **−** opens a **"Remove a Server" dialog listing every Server** on this Machine
(`Server 1: …`, `Server 2: …`, … `Server N: …`, each with its 🟢/⚪ dot and how many players are
online). **Nothing is preselected**: the user actively picks the one to remove, so it's never
simply the latest or the currently selected one. Servers that can't be removed right now are
shown greyed out with the reason next to them ("2 players online"). With only one Server, **−**
itself is greyed out ("You can't remove your only Server").

Removal is **permanent**: the chosen Server's folder (engine, Worlds, allowlist, permissions,
configuration) **and its backups** are deleted, and it leaves the list. Because that can't be
undone:

- After picking, the confirmation dialog states exactly what goes: the Server's label, its Worlds
  by name, and how many backups.
- The user must **type the Server's name** to enable the delete button.
- **Refused** while players are online (§5), and for the **last remaining** Server.
- A running Server is stopped first, as part of the same confirmed action.
- Afterwards the remaining Servers renumber on screen. Their folders, ports and player-visible
  names are untouched, so players' saved server entries keep working.

## 4. Automatic version check and a one-button Update

- The app asks Mojang's download-links endpoint
  (`https://net-secondary.web.minecraft-services.net/api/v1.0/download/links`), which returns the
  current release ZIP for Windows (`serverBedrockWindows`) and Linux (`serverBedrockLinux`), plus
  preview builds. Verified working from the homelab on 2026-10-11 (latest: 1.26.52.3). It is
  **undocumented**, though widely relied on (Docker images, the Arch Linux package). The app
  sends a browser-style User-Agent (the CDN rejects script ones) and **never picks a preview
  build** unless explicitly asked.
- **Update tab:** the three manual steps become one line plus one button:
  `Installed: 1.26.52.3 · Latest: 1.26.60.x — [Update now]` (or "✓ Up to date"). The existing
  confirmation stays, since Update replaces the Server's files.
- **Fallback:** if the endpoint fails or changes, the tab says so and offers today's manual way
  (Wiki + type the version), so the user is never stuck.
- **Installed version:** "Bedrock Server Version: Unknown" happens because the app looks for the
  version in files Mojang no longer ships with a number. From 3.0 the app records the version
  itself whenever it installs or updates (built as a marker file — see *Config changes*), with the old detection kept
  as a fallback.
- **"Update all Servers":** one download, one confirmation listing every Server, then each is
  updated in turn. A running Server is stopped before its turn and restarted after. A Server that
  fails is reported and doesn't stop the others.

## 5. Knowing who's online

Needed for "−", and useful on the strip (hover shows "2 players online") and in Fleet. Each
Server keeps a set of online player names from the console's `Player connected` /
`Player disconnected` lines (the same lines the Players tab already learns names from), cleared
when the Server stops. Network-level checks don't work for this: NetherNet players don't show up
as separate connections.

## 6. Cross-platform: Windows and Linux alike

The app runs and installs on both, so every part of 3.0 must work on both. The homelab being
Linux must not make the design Linux-shaped. Where they differ:

- **Downloads:** the version endpoint returns separate builds, so the app takes
  `serverBedrockWindows` on Windows and `serverBedrockLinux` on Linux (`sys.platform`), as the
  existing manual URL already does.
- **Starting a Server:** the existing per-OS start code is reused unchanged: `bedrock_server.exe`
  with no console window on Windows; `bedrock_server` made executable and `LD_LIBRARY_PATH` set
  on Linux. A freshly installed Linux Server needs the executable bit set before its first start.
- **Long paths on Windows:** Bedrock's resource packs are nested deep enough to pass Windows'
  260-character path limit when the install folder path is long. Backups already handle this
  (`_long_path`), but **the Update pipeline doesn't**: its extract, wipe and restore steps use
  plain paths (`perform_update`, e.g. the `zf.extract(member, server_path)` call). Since + installs
  through the same code, 3.0 **fixes this for both Update and +**. It also applies to deleting a
  Server's folder (§3).
- **Windows Firewall:** the first time a new Server starts, Windows asks whether to allow
  `bedrock_server.exe` (each Server has its own copy at its own path, so each new Server asks
  once). If the user declines, players can't connect. The + dialog says so beforehand ("Windows
  will ask to allow network access; choose Allow"), and the Server's first-start message repeats
  it on Windows. Linux has no equivalent prompt; a firewall, if any, is the user's own setup, and
  NetherNet needs **TCP** `server-port` as well as UDP (already in the User Guide).
- **Deleting a Server (§3):** on Windows a running `bedrock_server.exe` locks its files, so the
  Server must be fully stopped before its folder is deleted, and read-only files must not abort
  the delete halfway. The delete reports exactly what it couldn't remove rather than leaving a
  half-deleted Server listed.
- **Folders and Downloads:** new Server folders go next to Server 1's on either OS. The "reuse an
  already-downloaded ZIP" check uses the existing cross-platform Downloads lookup (registry on
  Windows, XDG on Linux).
- **The top-line strip:** Windows (often at 150% display scaling) and XFCE render fonts and
  button widths differently, so the shortening and "▾ N more" behaviour is checked on both.

## 7. Prerequisite: two NetherNet Servers on one machine

Since 1.26.5x each Server listens on **TCP** `server-port` for signalling and uses UDP for play,
plus **UDP 7551** for LAN discovery. Two Servers may collide on 7551, and Windows and Linux
handle shared ports differently, so the answer can differ per OS. **Step 0 tests both:** on the
**Windows dev PC** with two scratch Servers (no effect on anyone, any time), and on the **Linux
homelab** at a quiet moment and with the user's OK, running a second scratch Server next to the
real one. The rule below is applied per OS if the results differ:

- If both can stay LAN-visible, keep LAN visibility on for every Server.
- If not, the first Server keeps LAN visibility and the others are created with
  `enable-lan-visibility=false`. Players add them once by IP and port, and the + dialog shows
  exactly what to enter.

**Result (2026-10-11), identical on Windows 11 and on Linux (WSL Ubuntu 24.04), BDS 1.26.52.3:**
two NetherNet Servers run side by side without problems (both start, both sign in to the
signalling service, each listens on TCP at its own `server-port`). Only **one** gets UDP 7551, and
it's **whichever starts first**; the second silently binds a random UDP port instead, with no
error, so it runs but isn't LAN-discoverable. **Rule adopted:** exactly one Server per machine is
LAN-visible (Server 1 by default); + creates every other Server with
`enable-lan-visibility=false`, and its dialog shows the IP and port players add once. This makes
LAN visibility deterministic instead of depending on start order. (Inferred from the sockets each
Server holds; no Minecraft client was used. The homelab, on real hardware, is checked when 3.0 is
deployed there.)

**Also found:** a freshly installed 1.26.52.3 has `allow-list=true` with an empty allowlist, so
nobody can join until someone is added (the console warns about it). The + wizard sets
`allow-list=false` by default, the same as Server 1 runs today, and the Players tab is where to
turn it on.

## Config changes

- **None in the config.** The installed version is a marker file, `.bsm_bedrock_version`, in the
  Server's own folder (written by install/update and refreshed from the engine's `Version:`
  startup line), not a per-Server config key as first planned: it stays true when the folder is
  moved or adopted, and it's naturally covered by the update pipeline without a profile-shape
  change. Every 2.0.1 config key still loads; `last_zip_path` is simply no longer restored
  (a remembered old ZIP would take over the Update button).
- No migration needed for existing users: their one Server simply becomes "Server 1", with
  folder, port and settings unchanged.

## Build order (each step verified before the next)

0. NetherNet coexistence test on Windows and Linux (§7) — decides the LAN-visibility rule.
1. Version endpoint + recorded installed version + one-button Update tab, with manual fallback,
   plus the long-path fix in the Update pipeline (§6). (Useful on its own, even with one Server.)
2. Online-player tracking (§5).
3. Top-line Server strip with overflow (§1), sidebar trimmed.
4. "+" wizard (§2).
5. "−" removal (§3).
6. "Update all Servers" (§4).
7. Docs (GUI-DESIGN, USER-GUIDE, CHANGES, README, CLAUDE.md), bump to **3.0 "Servers"**
   (`APP_RELEASE_NAME`), release. The bug fixes already on `main` (atomic config save,
   corrupt-config recovery, log levels, crash logging) ship in the same release.

Verification follows the project's usual approach: scratch-folder tests against real code for the
logic, then **every step run on both operating systems**: on the Windows dev PC with scratch
Servers, and on the Linux homelab for the parts that touch real Servers, always asking before
anything is started, stopped or deleted there. The release builds both installers as before.
