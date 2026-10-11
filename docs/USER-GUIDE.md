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

1. Launch the app and press **+** at the top right. Give the Server a name (what players see),
   pick a game mode, and — if you like — type a **seed** for the World. The app downloads the
   newest Bedrock Dedicated Server itself and installs it; there's nothing to find or unzip.
   *(Already have a Bedrock server folder? In the same window, choose **Use an existing Bedrock
   folder instead…**.)*
2. You land on **📝 Configuration** to look over the settings (difficulty, max players, …).
3. On **🎮 Server**, hit **▶ Start**. The World is generated on this first start. The console at
   the bottom shows the engine's own output live. On Windows, allow `bedrock_server.exe` when
   the firewall asks, or players can't connect.

That's the whole loop for a single Server: Server tab to run it, Worlds/Players/Configuration/
Backups to manage it, Update when a new Bedrock Server Version comes out.

---

## The seven tabs

| Tab | What you do there |
|---|---|
| 🎮 **Server** | The home screen: what's running, Start/Stop/Restart, the Active World dropdown, **🎲 Gamerules**, and the live console (type commands at the bottom, or use the Quick buttons). |
| 🌍 **Worlds** | Create (name + optional seed), rename, delete Worlds; see each one's size and which Bedrock Server Version it last ran on (a World won't load on an older engine than that). Switch the Active World here. |
| 👥 **Players** | Allowlist (who's allowed to join), roles (visitor/member/operator), and per-player game mode (mix survival and creative players on one Server). Names are learned automatically as people join. |
| 📝 **Configuration** | The full `server.properties` editor — everything except `level-name`, which lives on the Active World dropdown instead. At the top, three checkboxes for settings Mojang's file leaves out: **telemetry**, **Vibrant Visuals** and a **chat log** (experimental) — see below. |
| 💾 **Backups** | What gets backed up (the preserve checklist), your backup policy (how many to keep, compress or not), and the backup list itself with restore/delete. |
| 🔄 **Update** | Shows `Installed: … · Latest: …` and one button — **⬆ Update to …** or **✓ Up to date**. With several Servers, **Update all Servers** does them all in one go. Backs up your preserved items first, always. **Local machines only** — see below. |
| ⚙️ **Settings** | App-level preferences, Remote Administration, and this Server's folder (to relocate it — adding a *new* Server is **+** at the top right, not this). |

**A tip that saves confusion:** a running Server greys out anything that would require
restarting it (like the Active World dropdown) — stop it first, or use the app's own
confirm-and-restart flows where offered.

---

## Managing more than one Server (optional)

Say the grown-ups play survival and the kids creative: make them two Servers. Each one is
completely separate — its own Worlds, settings, allowed players and backups — and they can run
at the same time.

- The top line lists them: **Server 1: \<Active World\>**, **Server 2: …** (🟢 = running). Click
  one to work on it; the tabs below follow. If they don't all fit, the rest are under
  **▾ N more**.
- **+** makes a new Server (see *First run*). You can start it from a new World (with an
  optional seed) or from a copy of an existing World.
- **Only the first Server shows up in Minecraft's LAN list.** That's a Bedrock limit: one per
  computer. Players add the others under **Servers → Add Server** with this computer's address
  and the port the app shows when the Server is created (19134, 19136, …).
- **−** removes a Server **for good**: choose which one, check the list of what will be deleted
  (its folder, its Worlds, its backups), and type the World's name to confirm. A Server with
  players on it can't be chosen. The numbers close up afterwards — remove Server 2, and Server 3
  becomes Server 2.
- **🔄 Update ▸ Update all Servers** updates every Server in one go: running ones are stopped
  and started again, and if one fails the others still get done.

For a bird's-eye view, click **▶ Machines** (top-left) to open the sidebar:
**🌐 Fleet (All Servers)** shows every Server across every Machine in one table, with
Start/Stop right there. Click **◀ Hide** to put it away again.

## Administering another computer's Servers (optional, LAN only)

You can pair with another computer on your home network and control its Servers from here,
using the exact same tabs as a local Server.

1. On the computer that will **host** the Servers: **⚙️ Settings ▸ Remote Administration**,
   enable it, note the port and the pairing token.
2. On the computer that will **administer**: **▶ Machines**, then the sidebar's **➕ Machine**, enter that computer's
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
- **The three checkboxes at the top of Configuration** (each takes effect when that Server next
  starts; a box you never touch isn't written to the file):
  - **Send server telemetry to Mojang** — off unless ticked. Bedrock prints a "Telemetry is
    currently not enabled" message at every start while it's off; that's normal and can't be
    silenced.
  - **Turn off Vibrant Visuals for players** — ticked by default, because that's Bedrock's own
    default on a dedicated server. **Untick it** if your players want Minecraft's new Vibrant
    Visuals graphics (on devices that support it).
  - **Save players' chat to a log file (experimental)** — writes chat to
    `chat_logs/ChatLog_<date>.txt` in that Server's folder. Mojang documents it only for
    Minecraft Education, so it may not work on a normal server.
- **About seeds:** a seed decides the landscape of a *new* World — the same seed gives the same
  World. It can't change a World that already exists. Leave it empty for a random one.

---

## Troubleshooting

- **Windows says "unknown publisher" / SmartScreen warning.** The installers aren't
  code-signed. Click *More info → Run anyway* if you trust the source you downloaded it from.
- **A Server won't start.** Check the console log at the bottom of the Server tab — the engine
  prints its own error there (often a port already in use, or a corrupt World). The port
  guard stops you from starting two of your *own* Servers on the same port, but something else
  on your machine using that port will still show up as an engine-side failure.
- **After updating, players get a "transport" error / the console shows TRANSPORT TYPE ERROR
  ("not set to NetherNet").** Bedrock 1.26.5x only accepts players over NetherNet, which needs
  `transport=nethernet` in `server.properties`. Updates made with this version of the app add it
  automatically; if you updated with an older one, the app offers to add it when it sees the
  error — say yes, then press **Restart**. NetherNet listens on **TCP** `server-port` (19132) plus
  UDP, so if you have a firewall, allow TCP 19132 as well as UDP. If LAN discovery doesn't show the
  Server, add it manually by IP address and port.
- **Where's the config file?** `~/.config/bedrock-updater/` on Linux, `%APPDATA%\bedrock-updater\`
  on Windows. Logs live right beside it.
- **I want the old single-Server look back permanently.** That's the default — the sidebar
  starts collapsed. If you've expanded it, just click **◀ Hide**.
- **I clicked the icon again and no new window appeared.** That's intentional: the app runs a
  single window. Launching it again (shortcut, taskbar, etc.) just brings the existing window to
  the front instead of opening a second copy — two copies would fight over the same settings and
  the same running server.
