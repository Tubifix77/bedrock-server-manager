# Linux Compatibility Fixes Summary

## What Was Changed

Your Bedrock Minecraft Server Manager has been updated for full Linux (Debian 12) compatibility!

---

## ✅ Key Fixes Applied

### 1. **Executable Permissions** 
- **Problem:** Linux requires +x permission on `bedrock_server`
- **Fix:** Added `make_executable()` function that automatically sets permissions
- **Applied in:**
  - Server startup
  - After extracting updates
  - After restoring backups
  - Validation tool

### 2. **Library Path (LD_LIBRARY_PATH)**
- **Problem:** Bedrock server needs to find its .so libraries
- **Fix:** Sets `LD_LIBRARY_PATH` environment variable when starting server
- **Location:** ServerManager.start() method

### 3. **Downloads Folder Detection**
- **Problem:** Windows-only registry code for finding Downloads
- **Fix:** Added XDG directory support for Linux
- **Fallback:** `~/Downloads` if XDG not configured

### 4. **File Paths**
- **Problem:** Potential issues with path separators
- **Fix:** Uses `Path` objects consistently (handles / vs \ automatically)

### 5. **Config & Log Locations**
- **Problem:** Windows-specific paths
- **Fix:** 
  - Config: `~/.config/bedrock-updater/`
  - Logs: `~/.local/share/bedrock-updater/logs/`
  - Follows Linux XDG standards

### 6. **File Browser**
- **Problem:** Windows-only `os.startfile()`
- **Fix:** Uses `xdg-open` on Linux (works with any DE)

### 7. **Version Update**
- Updated version string to "1.0.1-Linux"
- Added Linux-specific startup message

---

## 📦 Files in This Repo

1. **bedrock_updater_linux.py** - The main application (Linux-compatible)
2. **bedrock-server-manager.desktop** - Desktop launcher (points to ~/Bedrock/)
3. **README.md** - Project overview and quick reference
4. **CHANGES.md** - This file
