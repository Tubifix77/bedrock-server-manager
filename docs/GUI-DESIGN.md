# GUI design & terminology (as of 1.0.3)

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
| 1 | **🎮 Server** *(home)* | See & run the Server | **Active Server Information** (name, Bedrock Server Version, Active World + its last-run version, gamemode, port, worlds) · Start/Stop/Restart · **Active World dropdown** (existing Worlds only; greys out while running with an inline "Not available until the running Server is stopped" hint — no static helper text while stopped) · network info · console (half height) |
| 2 | **🌍 Worlds** | Create & manage Worlds | **✨ Create New World** (hero button — where new users land after a fresh install) · Worlds list with sizes, dates, **Last Run On** · Set Active / Rename / Delete (all refuse a running Server; Delete refuses the Active World) |
| 3 | **📝 Active Server Configuration** | Edit the Server | `server.properties` editor (skips `level-name` — that's the Active World). Natural stop between creating a World and first start (seed, gamemode…). |
| 4 | **💾 Backups** | Per-Server backups | Header **names the Server** and where backups live · **What to back up** checklist (the preserve list — same list updates preserve) · backup list (~⅓ height) · restore/delete |
| 5 | **🔄 Update** | Swap the Bedrock Server Version | Installed version · ZIP picker · steps **1: Wiki Version → 2: Download Latest → 3: Update Server** · Dry Run / Open Folder (small, right) · activity log |
| 6 | **⚙️ Settings** | App-level | **Server Location** (the Server Folder — one-time setup) · backup/update/interface prefs · dark mode |

## Rationale (decisions, so they don't get relitigated)

- **Server is home**: status + control + "what am I running" belong on the landing tab.
  Update is a maintenance tool, not the home screen.
- **The Server Folder is configuration**, not a daily control → Settings. First-run guidance:
  info panel and log point to ⚙️ Settings; fresh installs point to 🌍 Worlds.
- **Dropdown over free text** for the Active World: only real folders are offered, so a typo
  can't silently create an empty world. Creating (which legitimately needs free text) is an
  explicit, separate flow on Worlds.
- **"Active Server ___" prefix pairs the two panels**: *Active Server Information* (read) on
  Server, *Active Server Configuration* (edit) as its own tab. "Active" refers to Worlds only
  otherwise.
- **The preserve list lives with Backups** because "what a backup contains" and "what an
  update preserves" are the same list — Update reads it from there.
- **"Worlds", not "New"**: in a *server manager*, a tab called "New" reads as "new Server" —
  which is a different (future) feature.
- **No auto-stop side effects** (user decision, July 2026): World actions never stop a running
  Server for you — controls grey out with an inline reason instead. Stopping is always an
  explicit click. Corollary: prefer state-driven hints that appear only when relevant over
  permanent helper sentences.
