# Bedrock Server Manager for Linux

A comprehensive GUI application for managing Minecraft Bedrock Dedicated Servers on Linux (Debian 12).

![Version](https://img.shields.io/badge/version-1.0.2--Linux-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Platform](https://img.shields.io/badge/platform-Linux-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

---

## ✨ Features

- **Start/Stop/Restart** server with one click, live console output
- **Safe updates** — automatic backup before every update, selective file preservation
- **Backup management** — create, restore, auto-cleanup, optional compression
- **World browser** — view worlds, switch active world, see sizes and dates
- **Server properties editor** — edit `server.properties` in GUI
- **Cross-platform** — works on Linux (primary) and Windows

---

## 📋 Requirements

- Debian 12 (Bookworm) or similar Debian-based distro
- Python 3.11+
- `python3-tk` (`sudo apt install python3-tk`)

---

## 🚀 Quick Start

```bash
sudo apt install python3 python3-tk -y
chmod +x bedrock_updater_linux.py
python3 bedrock_updater_linux.py
```

To install the desktop launcher, copy `bedrock-server-manager.desktop` to `~/.local/share/applications/` and update the `Exec=` path if needed.

---

## 📁 Files

| File | Description |
|------|-------------|
| `bedrock_updater_linux.py` | Main application |
| `bedrock-server-manager.desktop` | Desktop launcher |
| `CHANGES.md` | Linux compatibility change log |

---

## 🔧 Configuration

Stored at `~/.config/bedrock-updater/.bedrock_updater_config.json`. Logs at `~/.local/share/bedrock-updater/logs/`.

---

## 👤 Author

Tue Wincentz Boas — Built with Claude AI & Gemini 3

MIT License
