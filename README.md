# Bedrock Server Manager

A comprehensive cross-platform GUI application for managing Minecraft Bedrock Dedicated Servers.

![Version](https://img.shields.io/badge/version-1.0.2-blue)
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

- **Start/Stop/Restart** server with one click, live console output
- **Safe updates** — automatic backup before every update, selective file preservation
- **Backup management** — create, restore, auto-cleanup, optional compression
- **World browser** — view worlds, switch active world, see sizes and dates
- **Server properties editor** — edit `server.properties` in GUI
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

---

## 📁 Files

| File | Description |
|------|-------------|
| `bedrock_updater_linux.py` | Main application |
| `bedrock-server-manager.desktop` | Linux desktop launcher |
| `packaging/` | Build scripts for the Windows installer & Linux AppImage |
| `CHANGES.md` | Changelog |

---

## 🔧 Configuration

Stored at `~/.config/bedrock-updater/` (Linux) or `%APPDATA%\bedrock-updater\` (Windows). Logs in the same folder.

---

## 👤 Author

Tue Wincentz Boas — Built with Claude AI & Gemini 3

MIT License
