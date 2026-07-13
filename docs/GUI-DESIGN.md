# GUI design & terminology (as of 1.0.4)

The naming convention and tab layout agreed for this app. Use these words consistently —
in the GUI, in docs, and when discussing changes. The point is to undo Bedrock's own
server/world mix-up (`server.properties` holds both `server-name` and `level-name`).

## Terminology

| Term | Means | Notes |
|------|-------|-------|
| **Bedrock Server Version** | The engine build (`bedrock_server` + libs + stock packs). | Mojang's own term. It's what **Update** swaps. Never call this "the server". |
| **Server** | One configured install: name + port + config + its Worlds. | One per install; "multiple servers" = multiple installs on different ports (future: `server_profiles`). The path field stays **"Server Folder"**. |
| **World** | A save under `worlds/<name>/`, belonging to a Server. | A Server has many; switching never touches the others. |
| **Active World** | The one World the Server loads — the `level-name` pointer. | Set via the dropdown on the Server tab. Changing it takes effect on next start. |
| **backup** | A copy of a Server's Worlds + config (the preserve list). | Kept lowercase/unchanged — Mojang doesn't use "snapshot", and in Minecraft "snapshot" means Java dev builds. Backups are **per Server**, contain **all** Worlds, and restore **wholesale**. |

Key sentence: **a running Server = one World loaded on one Bedrock Server Version.**

**Per-World version:** each World's `level.dat` carries `lastOpenedWithVersion` (NBT) — the
save format ratchets up to whatever version last ran it, and **won't load on older versions**.
Shown as "Last Run On" in the Worlds list and on the Active World line in Active Server
Information (with ⚠ when a World is newer than the installed Bedrock Server Version).

## Tab layout (order = daily use first)

| # | Tab | Role | Holds |
|---|-----|------|-------|
| 1 | **🎮 Server** *(home)* | See & run the Server | **Active Server Information** (name, Bedrock Server Version, Active World + its last-run version, gamemode, port, worlds) · Start/Stop/Restart · **🎲 Gamerules** dialog (reads the Active World's `level.dat`, applies live via `gamerule` commands — gamerules are per-World and are *not* `server.properties`) · **Active World dropdown** (existing Worlds only, plain names — the selected entry *is* the active World, no extra "(active)" tag; greys out while running with an inline "Not available until the running Server is stopped" hint — no static helper text while stopped) · network info · console (half height) |
| 2 | **🌍 Worlds** | Create & manage Worlds | **✨ Create New World** (hero button — where new users land after a fresh install; ends by jumping to Active Server Configuration) · Worlds list with sizes, dates, **Last Run On** — the Active World is marked **✅ ACTIVE**; a created-but-not-yet-generated Active World shows as an orange *"created on next start"* pending row · **Set as Active World** (if the Server runs: confirm → stop nicely → switch → start again) · Rename / Delete (refuse a running Server; Delete refuses the Active World) |
| 3 | **👥 Players** | Manage people | **Access**: allowlist editor + "restrict joining" toggle (writes `allow-list`, live `allowlist on/off`; empty-list warning; Bedrock has **no blacklist**) · **Roles**: `permissions.json` visitor/member/operator (live `permission reload`) · **Game mode per player** (live `gamemode` command → mixed modes; warns if `force-gamemode=true`). Names+XUIDs auto-learned from join lines into `known_players` (tolerant regex — wording varies by version), with a **🔍 Scan console** button that re-reads the whole buffer and backfills missing allowlist XUIDs. |
| 4 | **📝 Configuration** | Edit the Server | Page header: **Active Server Configuration**. `server.properties` editor (skips `level-name` — that's the Active World). Natural stop between creating a World and first start (seed, gamemode…). |
| 5 | **💾 Backups** | Per-Server backups | Header **names the Server** and where backups live · **What to back up** checklist (the preserve list — same list updates preserve) · backup list (~⅓ height) · restore/delete |
| 6 | **🔄 Update** | Swap the Bedrock Server Version | Installed version · ZIP picker · steps **1: Wiki Version → 2: Download Latest → 3: Update Server** · Dry Run / Open Folder (small, right) · activity log |
| 7 | **⚙️ Settings** | App-level | **Server Location** (the Server Folder — one-time setup) · backup/update/interface prefs · dark mode |

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
