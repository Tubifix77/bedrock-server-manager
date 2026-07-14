# Bedrock Server Manager

A comprehensive cross-platform GUI application for managing Minecraft Bedrock Dedicated
Servers — one, or a whole fleet across your home network.

![Version](https://img.shields.io/badge/version-2.0.0-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## ⬇️ Download / Install

**Prebuilt, no Python needed** — grab the latest from the [Releases page](https://github.com/Tubifix77/bedrock-server-manager/releases):

- **Windows:** `BedrockServerManager-<version>-Setup.exe` — installs anywhere you choose, adds Start-menu / desktop shortcuts.
- **Linux:** `BedrockServerManager-<version>-x86_64.AppImage` — `chmod +x` and run anywhere.

> Artifacts are unsigned, so Windows SmartScreen shows an "unknown publisher" prompt (**More info → Run anyway**).

Prefer to run from source? See **Quick Start** below. Build/release details are in [`packaging/README.md`](packaging/README.md).

---

## ✨ Features

- **Manage one Server, or many** — a sidebar lists every configured Server; several can run
  simultaneously (on different ports), and a **🌐 Fleet overview** shows all of them across
  every machine at a glance
- **Remote administration over your LAN** — pair another PC (host/IP + a pairing token) and
  administer its Servers from every tab exactly like a local one: live console, start/stop,
  commands, worlds, players, configuration, backups. Or run a machine headless with `--agent`
- **Server home tab** — Active Server Information at a glance, Start/Stop/Restart, live console
- **Multi-world management** — create, switch (dropdown), rename and delete Worlds; shows each World's *last run on* version so you know what it needs
- **Player management** — allowlist (who may join), roles (visitor/member/operator), and **per-player game mode** so survival and creative players share one Server
- **Gamerules** — one-sleeper night skip, keep inventory, mob griefing, daylight cycle and more, read from the World and applied live
- **Safe updates** — automatic backup before every update, selective file preservation
- **Backup management** — per-Server backups (namespaced so Servers never mix): create, restore, auto-cleanup, optional compression
- **Server configuration editor** — edit `server.properties` in the GUI, locally or on a paired remote Server
- **Cross-platform** — Windows and Linux

---

## 📋 Requirements (running from source)

- Python 3.11+
- `tkinter` — on Debian/Ubuntu: `sudo apt install python3-tk`

---

## 🚀 Quick Start (from source)

**Linux:**
```bash
sudo apt install python3 python3-tk -y
python3 bedrock_updater_linux.py
```

**Windows:**
```powershell
python bedrock_updater_linux.py
```

To install the desktop launcher on Linux, copy `bedrock-server-manager.desktop` to `~/.local/share/applications/` and update the `Exec=` path if needed.

### Remote administration

Any install can be paired: on the machine hosting Servers, go to **⚙️ Settings ▸ Remote
Administration**, enable it, and note the port + pairing token. On the administering machine,
use the sidebar's **➕ Machine** and enter that host's address, port, and token.

To run a headless host (no GUI needed on that machine — e.g. a machine without a desktop):

```bash
python3 bedrock_updater_linux.py --agent [--config PATH] [--port N]
```

This is **LAN-only by design** — a plaintext session after an authenticated pairing
handshake. Don't port-forward it; use a VPN (e.g. Tailscale/WireGuard) if you need to reach a
Machine off your home network.

---

## 📁 Files

| File | Description |
|------|-------------|
| `bedrock_updater_linux.py` | Main application |
| `bedrock-server-manager.desktop` | Linux desktop launcher |
| `packaging/` | Build scripts for the Windows installer & Linux AppImage |
| `docs/GUI-DESIGN.md` | GUI layout & terminology reference |
| `docs/V2-MAJORDOMO-PLAN.md` | 2.0 multi-Server/multi-Machine design + build history |
| `CHANGES.md` | Changelog |

---

## 🔧 Configuration

Stored at `~/.config/bedrock-updater/` (Linux) or `%APPDATA%\bedrock-updater\` (Windows). Logs in the same folder.

---

## 👤 Author

Tue Wincentz Boas — Built with Claude AI & Gemini 3

MIT License
