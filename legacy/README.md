# Legacy / archived versions

Older builds of the Bedrock server manager, kept for reference and history. **None of these
are maintained** — the current, shipping version is
[`bedrock_updater_linux.py`](../bedrock_updater_linux.py) (`1.0.1-Linux`) in the repo root.

> ⚠️ **The version numbers below are not a linear sequence.** These were parallel experiments.
> The `2.5.0` file is an *older, separate* line (built in a Microsoft Copilot chat) despite its
> higher number — the project was later restarted at `1.0.0` under the "Bedrock Server Manager"
> name, and that line is what became today's `1.0.1-Linux`.

| File | Version | Name | Origin | Notes |
|------|---------|------|--------|-------|
| [`bedrock_updater_newworldedition.py`](bedrock_updater_newworldedition.py) | `1.0.0` | Bedrock Server Manager | Direct ancestor of current | The immediate predecessor of `1.0.1-Linux`, **before** the Linux-compatibility pass. |
| [`bedrock_updater_pro_ultimate_versioncheck.py`](bedrock_updater_pro_ultimate_versioncheck.py) | `2.5.0` | Bedrock Server Updater Pro | Microsoft Copilot experiment | A richer parallel branch with features the current line never adopted. |

## `bedrock_updater_newworldedition.py` — v1.0.0

The version the current shipping code grew out of. Same 6-tab layout
(Update / Server / Backups / Worlds / Properties / Settings), the `ServerPropertiesEditor`
key/value editor, and the Worlds-tab "Active World Management" (set/create the active world via
`level-name`).

What it lacks vs. the current `1.0.1-Linux` — i.e. the Linux-compatibility work added afterward
(see [`../CHANGES.md`](../CHANGES.md)):

- No `LD_LIBRARY_PATH` set when launching `bedrock_server` on Linux.
- No `make_executable()` helper (Linux `chmod +x` after extract/restore).
- `get_downloads_folder()` is Windows-registry only, falling back to `~/Downloads` (no XDG lookup).
- No "Running on Linux" startup handling.

## `bedrock_updater_pro_ultimate_versioncheck.py` — v2.5.0 "Pro"

A more ambitious, separate experiment. Shares the same core (`BackupManager`, the observer-pattern
`ServerManager`, the destructive backup → wipe → extract → restore update pipeline) but adds a lot
the current line does not have:

- **`VersionChecker`** — scrapes the Minecraft Wiki for the latest *Release* version, builds the
  `bin-win` / `bin-linux` CDN download URL automatically, and downloads with progress. (The current
  `1.0.1-Linux` instead asks the user to type the version in manually.)
- **Automation timers** — optional scheduled auto-backup and periodic update checks, plus a
  one-click "check → download → update" flow and an in-app "update available" banner.
- **Players tab** — editors for `allowlist.json` and `permissions.json` (add player / add operator).
- **Typed Properties editor** — driven by a `SERVER_PROPERTIES_INFO` table, so each setting renders
  as a dropdown / checkbox / bounded number field with a description, rather than a raw text entry.
- Misc: a top menu bar, server-uptime display, and a `check_port_open()` port checker.

These are the obvious candidates if features are ever ported forward into the current line.
