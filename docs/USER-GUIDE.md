# User Guide

A plain walkthrough of everyday use. For GUI layout rationale and terminology aimed at
contributors, see [`GUI-DESIGN.md`](GUI-DESIGN.md) instead — this page is for using the app,
not building it.

**Terms used throughout:** a **Machine** is a computer (this one, or another on your network);
a Machine hosts **Servers**; a Server is one Bedrock Dedicated Server install (its own folder,
port, and settings); a Server holds one or more **Worlds**, and exactly one is **Active**
(the one it actually runs); the **Bedrock Server Version** is the game engine itself — the
thing Update swaps out, distinct from your Worlds, which survive an update untouched.

---

## First run

1. Launch the app. On Server tab, if nothing is configured yet, the info panel points you to
   **⚙️ Settings** to set your Server's folder — the folder containing `bedrock_server`
   (or `bedrock_server.exe` on Windows) and `server.properties`. If you don't have a Bedrock
   Dedicated Server yet, grab one from the official Minecraft site and unzip it somewhere first.
2. Once a folder is set, head to **🌍 Worlds** and use **✨ Create New World** — this is where
   new installs land, since a fresh Bedrock install has no World yet.
3. Back on **🎮 Server**, hit **▶ Start**. The console at the bottom shows the engine's own
   output live.

That's the whole loop for a single Server: Server tab to run it, Worlds/Players/Configuration/
Backups to manage it, Update when a new Bedrock Server Version comes out.

---

## The seven tabs

| Tab | What you do there |
|---|---|
| 🎮 **Server** | The home screen: what's running, Start/Stop/Restart, the Active World dropdown, **🎲 Gamerules**, and the live console (type commands at the bottom, or use the Quick buttons). |
| 🌍 **Worlds** | Create, rename, delete Worlds; see each one's size and which Bedrock Server Version it last ran on (a World won't load on an older engine than that). Switch the Active World here. |
| 👥 **Players** | Allowlist (who's allowed to join), roles (visitor/member/operator), and per-player game mode (mix survival and creative players on one Server). Names are learned automatically as people join. |
| 📝 **Configuration** | The full `server.properties` editor — everything except `level-name`, which lives on the Active World dropdown instead. |
| 💾 **Backups** | What gets backed up (the preserve checklist), your backup policy (how many to keep, compress or not), and the backup list itself with restore/delete. |
| 🔄 **Update** | Swap the Bedrock Server Version: paste the new version's ZIP path, hit Update. Backs up your preserved items first, always. **Local machines only** — see below. |
| ⚙️ **Settings** | App-level preferences, Remote Administration, and this Server's folder (to relocate it — adding a *new* Server is the sidebar's ➕ Server, not this). |

**A tip that saves confusion:** a running Server greys out anything that would require
restarting it (like the Active World dropdown) — stop it first, or use the app's own
confirm-and-restart flows where offered.

---

## Managing more than one Server (optional)

If you only ever run one Server, you can ignore this whole section — the app opens with the
sidebar hidden, looking just like the simple single-Server layout.

Click **▶ Machines** (top-left, always there) to reveal it:

- **➕ Server** adds another, independent local Server (its own folder, settings, backups).
  Several can run at once, on different ports — the app refuses to start two Servers on the
  same port so they can't collide.
- **🌐 Fleet (All Servers)** shows every Server across every Machine in one table — a
  bird's-eye view, with Start/Stop right there.
- Click **◀ Hide** to put the sidebar away again.

## Administering another computer's Servers (optional, LAN only)

You can pair with another computer on your home network and control its Servers from here,
using the exact same tabs as a local Server.

1. On the computer that will **host** the Servers: **⚙️ Settings ▸ Remote Administration**,
   enable it, note the port and the pairing token.
2. On the computer that will **administer**: sidebar's **➕ Machine**, enter that computer's
   address, port, and token, then Test Connection.
3. Its Servers now appear in your sidebar under that Machine's name, driven identically to a
   local Server — console, start/stop, worlds, players, configuration, backups.

A machine can also run **headless** (no window at all — handy for something without a
monitor):

```bash
python3 bedrock_updater_linux.py --agent [--config PATH] [--port N]
```

**This is LAN-only by design** — a plaintext session after an authenticated pairing handshake,
not meant to be exposed to the internet. If you need to reach a Machine away from home, put it
behind a VPN (Tailscale or WireGuard both work well) rather than forwarding the port.

**Updating the Bedrock Server Version only works on the local machine you're sitting at,
never remotely** — it's a destructive operation (it wipes the Server folder and replaces it),
and doing that blind over a network link isn't worth the risk if the connection drops
mid-copy. Walk over to (or remote-desktop into) the machine itself to run an update on it.

---

## Backups and updates, in plain terms

- A backup only saves what's checked in the Backups tab's preserve list — Worlds are marked
  critical and checked by default; make sure anything else you care about (custom resource/
  behavior packs, `allowlist.json`, etc.) stays checked too.
- **Update always backs up your preserved items first**, then wipes the Server folder and
  installs the new version, then restores what it backed up. If something ever goes wrong
  mid-update, your last backup is sitting in the Backups tab.
- A fresh install (no `server.properties` yet) skips the backup/wipe steps entirely — there's
  nothing to lose yet.

---

## Troubleshooting

- **Windows says "unknown publisher" / SmartScreen warning.** The installers aren't
  code-signed. Click *More info → Run anyway* if you trust the source you downloaded it from.
- **A Server won't start.** Check the console log at the bottom of the Server tab — the engine
  prints its own error there (often a port already in use, or a corrupt World). The port
  guard stops you from starting two of your *own* Servers on the same port, but something else
  on your machine using that port will still show up as an engine-side failure.
- **Where's the config file?** `~/.config/bedrock-updater/` on Linux, `%APPDATA%\bedrock-updater\`
  on Windows. Logs live right beside it.
- **I want the old single-Server look back permanently.** That's the default — the sidebar
  starts collapsed. If you've expanded it, just click **◀ Hide**.
- **I clicked the icon again and no new window appeared.** That's intentional: the app runs a
  single window. Launching it again (shortcut, taskbar, etc.) just brings the existing window to
  the front instead of opening a second copy — two copies would fight over the same settings and
  the same running server.
