#!/usr/bin/env python3
"""
Bedrock Server Updater Pro Ultimate
A comprehensive cross-platform tool for managing Minecraft Bedrock Dedicated Servers.
Features: Update, backup, restore, run server, auto-cleanup, download updates, and more.
"""

import os
import sys
import json
import copy
import uuid
import shutil
import zipfile
import hashlib
import threading
import subprocess
import socket
import signal
import re
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter import font as tkfont
from pathlib import Path
import logging
from typing import Optional, Dict, List, Tuple
from collections import deque
import time

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

APP_NAME = "Bedrock Server Manager"
APP_VERSION = "1.0.4-Linux"
APP_AUTHOR = "Tue Wincentz Boas - Built with Claude AI & Gemini 3"
CONFIG_FILENAME = ".bedrock_updater_config.json"
MINECRAFT_DOWNLOAD_PAGE = "https://www.minecraft.net/en-us/download/server/bedrock"

# Files/folders to preserve during update
DEFAULT_PRESERVE_ITEMS = {
    "worlds": {"enabled": True, "description": "World save data (critical!)", "critical": True},
    "server.properties": {"enabled": True, "description": "Server configuration"},
    "allowlist.json": {"enabled": True, "description": "Allowed players list"},
    "permissions.json": {"enabled": True, "description": "Player permissions/ops"},
    "valid_known_packs.json": {"enabled": True, "description": "Resource/behavior pack registry"},
    "resource_packs": {"enabled": True, "description": "Custom resource packs"},
    "behavior_packs": {"enabled": True, "description": "Custom behavior packs"},
    "world_templates": {"enabled": True, "description": "World templates"},
    "development_resource_packs": {"enabled": False, "description": "Development resource packs"},
    "development_behavior_packs": {"enabled": False, "description": "Development behavior packs"},
    "config": {"enabled": True, "description": "Additional config folder"},
}

# Config schema version. v2 ("Majordomo") introduces server_profiles — see
# docs/V2-MAJORDOMO-PLAN.md. migrate_config_to_v2() upgrades an old flat
# config the first time it's loaded.
CONFIG_VERSION = 2

# Default settings
DEFAULT_SETTINGS = {
    "config_version": CONFIG_VERSION,
    "last_zip_path": "",
    "last_server_path": "",
    "preserve_items": DEFAULT_PRESERVE_ITEMS,
    "max_backups": 5,
    "compress_backups": False,
    "auto_cleanup_backups": True,
    "auto_stop_server_before_update": True,
    "auto_start_server_after_update": False,
    "start_minimized_to_tray": False,
    "show_notifications": True,
    "dark_mode": False,
    "window_geometry": "900x700",
    "check_updates_on_start": True,
    "server_profiles": {},
    "active_profile": None,
    "machines": [],
    "remote_admin": {"enabled": False, "port": 19190, "token": ""},
    "console_font_size": 9,
    "console_max_lines": 1000,
    "known_players": {},
}

def _peek_server_name(server_path: Path) -> Optional[str]:
    """Quick, migration-only read of server-name (no app instance needed)."""
    props_file = server_path / "server.properties"
    if not props_file.exists():
        return None
    try:
        for line in props_file.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("server-name="):
                return line.split("=", 1)[1].strip() or None
    except Exception:
        pass
    return None


def migrate_config_to_v2(config: dict) -> dict:
    """Upgrade a 1.x flat config to v2's server_profiles model.

    Only called when the saved file predates config_version 2. Builds one
    profile from the old single-Server flat keys, then drops those keys —
    hydrate_active_profile_cache() repopulates them every load from the
    profile, so nothing else has to change for Stage 1 (see
    docs/V2-MAJORDOMO-PLAN.md, "Config v2 & migration").
    """
    last_path = (config.get("last_server_path") or "").strip()
    profiles = {}
    active_id = None
    if last_path:
        profile_id = uuid.uuid4().hex[:8]
        name = _peek_server_name(Path(last_path)) or Path(last_path).name or "Server"
        profiles[profile_id] = {
            "name": name,
            "path": last_path,
            "preserve_items": copy.deepcopy(config.get("preserve_items") or DEFAULT_PRESERVE_ITEMS),
            "max_backups": config.get("max_backups", 5),
            "compress_backups": config.get("compress_backups", False),
            "auto_cleanup_backups": config.get("auto_cleanup_backups", True),
            "auto_stop_server_before_update": config.get("auto_stop_server_before_update", True),
            "auto_start_server_after_update": config.get("auto_start_server_after_update", False),
            "known_players": copy.deepcopy(config.get("known_players") or {}),
        }
        active_id = profile_id
    for key in ("last_server_path", "preserve_items", "max_backups", "compress_backups",
                "auto_cleanup_backups", "auto_stop_server_before_update",
                "auto_start_server_after_update", "known_players"):
        config.pop(key, None)
    config["config_version"] = CONFIG_VERSION
    config["server_profiles"] = profiles
    config["active_profile"] = active_id
    return config


def hydrate_active_profile_cache(config: dict) -> dict:
    """Populate the flat 'current profile' keys from the active profile.

    Stage 1 still has exactly one selected Server; the flat keys
    (last_server_path, preserve_items, known_players, max_backups, ...) are
    its working cache, read by the existing widgets/methods unchanged.
    preserve_items/known_players are aliased (same dict object as the
    profile's) so in-session edits need no extra sync; scalars are re-synced
    into the profile at save time by
    BedrockUpdaterApp._sync_flat_settings_into_active_profile().
    """
    profile_id = config.get("active_profile")
    profiles = config.get("server_profiles") or {}
    profile = profiles.get(profile_id) if profile_id else None
    if not profile:
        config["last_server_path"] = ""
        return config
    config["last_server_path"] = profile.get("path", "")
    config["preserve_items"] = profile.setdefault("preserve_items", copy.deepcopy(DEFAULT_PRESERVE_ITEMS))
    config["known_players"] = profile.setdefault("known_players", {})
    for key, default in (("max_backups", 5), ("compress_backups", False),
                          ("auto_cleanup_backups", True),
                          ("auto_stop_server_before_update", True),
                          ("auto_start_server_after_update", False)):
        config[key] = profile.get(key, default)
    return config

# Gamerules surfaced in the Gamerules dialog (they live per-World, set via the
# `gamerule` console command — NOT in server.properties).
COMMON_GAMERULES = {
    "playerssleepingpercentage": ("int", 100),
    "keepinventory": ("bool", False),
    "showcoordinates": ("bool", False),
    "pvp": ("bool", True),
    "mobgriefing": ("bool", True),
    "dodaylightcycle": ("bool", True),
    "doweathercycle": ("bool", True),
    "dofiretick": ("bool", True),
    "tntexplodes": ("bool", True),
    "doinsomnia": ("bool", True),
    "falldamage": ("bool", True),
}

# Matches the name + XUID in BDS console lines. Deliberately tolerant: the exact
# wording varies between Bedrock Server Versions ("Player connected: Name, xuid: N",
# "Player Spawned: Name xuid: N, pfid: ...", disconnect lines, case differences).
PLAYER_XUID_RE = re.compile(
    r"Player\s+(?:connected|disconnected|spawned)\s*:\s*(.+?)\s*,?\s*xuid:\s*(\d+)",
    re.IGNORECASE)

SERVER_SIGNATURE_FILES = ["bedrock_server.exe", "bedrock_server", "server.properties"]
SERVER_EXECUTABLE = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def get_config_path() -> Path:
    if sys.platform == "win32":
        config_dir = Path(os.environ.get("APPDATA", Path.home()))
    else:
        config_dir = Path.home() / ".config" / "bedrock-updater"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir / CONFIG_FILENAME

def get_log_dir() -> Path:
    if sys.platform == "win32":
        log_dir = Path(os.environ.get("APPDATA", Path.home())) / "bedrock-updater-logs"
    else:
        log_dir = Path.home() / ".local" / "share" / "bedrock-updater" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

def load_config() -> dict:
    config_path = get_config_path()
    config = copy.deepcopy(DEFAULT_SETTINGS)
    raw_saved = None
    try:
        if config_path.exists():
            with open(config_path, 'r') as f:
                raw_saved = json.load(f)
            for key, value in raw_saved.items():
                if key in config:
                    if isinstance(config[key], dict) and isinstance(value, dict):
                        config[key].update(value)
                    else:
                        config[key] = value
    except Exception:
        pass
    was_v1 = raw_saved is not None and raw_saved.get("config_version", 1) < 2
    if was_v1:
        config = migrate_config_to_v2(config)
        try:
            bak_path = config_path.parent / (config_path.name + ".v1.bak")
            if not bak_path.exists():
                with open(bak_path, 'w') as f:
                    json.dump(raw_saved, f, indent=2)
        except Exception:
            pass
    return hydrate_active_profile_cache(config)

def save_config(config: dict):
    try:
        with open(get_config_path(), 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

def get_downloads_folder() -> str:
    """Get the user's Downloads folder - cross-platform compatible."""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
                return winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
        except Exception:
            pass
    
    # Linux/Unix fallback - check common locations
    downloads_path = Path.home() / "Downloads"
    
    # Try XDG user dirs if available
    try:
        xdg_config = Path.home() / ".config" / "user-dirs.dirs"
        if xdg_config.exists():
            with open(xdg_config, 'r') as f:
                for line in f:
                    if line.startswith('XDG_DOWNLOAD_DIR'):
                        # Parse the line like: XDG_DOWNLOAD_DIR="$HOME/Downloads"
                        path_str = line.split('=')[1].strip().strip('"')
                        path_str = path_str.replace('$HOME', str(Path.home()))
                        downloads_path = Path(path_str)
                        break
    except Exception:
        pass
    
    # Create if doesn't exist
    downloads_path.mkdir(parents=True, exist_ok=True)
    return str(downloads_path)

def get_file_hash(filepath: Path) -> str:
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()

def get_folder_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass
    return total

def format_size(size_bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"

def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"

def is_valid_bedrock_server(path: str) -> Tuple[bool, str]:
    path = Path(path)
    if not path.exists() or not path.is_dir():
        return False, "Path does not exist or is not a folder"
    found = [f for f in SERVER_SIGNATURE_FILES if (path / f).exists()]
    if not found:
        return False, "No Bedrock server files found"
    return True, f"Valid server ({', '.join(found)})"

def is_valid_bedrock_zip(zip_path: str) -> Tuple[bool, str]:
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            names = zf.namelist()
            for sig in SERVER_SIGNATURE_FILES:
                if sig in names or any(n.endswith('/' + sig) for n in names):
                    return True, "Valid Bedrock server ZIP"
    except zipfile.BadZipFile:
        return False, "Invalid or corrupted ZIP file"
    except Exception as e:
        return False, f"Error: {str(e)}"
    return False, "Not a Bedrock server ZIP"

def detect_server_version(server_path: Path) -> str:
    # Try version.txt first
    version_file = server_path / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text().strip()
        except Exception:
            pass
    # Try to extract from changelog
    changelog = server_path / "release-notes.txt"
    if changelog.exists():
        try:
            content = changelog.read_text()
            match = re.search(r'v?(\d+\.\d+\.\d+\.?\d*)', content)
            if match:
                return match.group(1)
        except Exception:
            pass
    return "Unknown"

def parse_version_tuple(version_str: str) -> Tuple[int, ...]:
    """'1.26.32.02' -> (1, 26, 32, 2) for safe comparisons."""
    try:
        parts = re.findall(r'\d+', str(version_str))
        return tuple(int(p) for p in parts[:4]) if parts else ()
    except Exception:
        return ()

def get_world_last_opened_version(world_dir: Path) -> str:
    """Read lastOpenedWithVersion from a Bedrock world's level.dat.

    level.dat is little-endian NBT: the tag appears as
    TAG_List(0x09) + name-length(int16 LE) + name, then item type
    TAG_Int(0x03) + count(int32 LE) + count*int32 LE version parts.
    A world won't load on a Bedrock Server Version older than this.
    """
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        return "Unknown"
    try:
        data = level_dat.read_bytes()
        idx = data.find(b"lastOpenedWithVersion")
        if idx < 3 or data[idx - 3] != 9:
            return "Unknown"
        pos = idx + len(b"lastOpenedWithVersion")
        item_type = data[pos]
        count = int.from_bytes(data[pos + 1:pos + 5], "little")
        if item_type != 3 or not (1 <= count <= 6):
            return "Unknown"
        pos += 5
        nums = [int.from_bytes(data[pos + i * 4:pos + i * 4 + 4], "little", signed=True)
                for i in range(count)]
        while len(nums) > 3 and nums[-1] == 0:
            nums.pop()
        return ".".join(str(n) for n in nums)
    except Exception:
        return "Unknown"

def read_world_gamerules(world_dir: Path) -> Dict[str, object]:
    """Best-effort read of COMMON_GAMERULES from a world's level.dat.

    Same little-endian NBT byte-scan as get_world_last_opened_version:
    tag-type byte, int16 name length, name, then payload (TAG_Byte for
    bools, TAG_Int for ints). Values are as of the world's last save.
    """
    values = {}
    level_dat = world_dir / "level.dat"
    if not level_dat.exists():
        return values
    try:
        data = level_dat.read_bytes()
        for rule, (kind, _default) in COMMON_GAMERULES.items():
            key = rule.encode()
            idx = data.find(key)
            while idx != -1:
                if idx >= 3 and int.from_bytes(data[idx - 2:idx], "little") == len(key):
                    tag = data[idx - 3]
                    pos = idx + len(key)
                    if tag == 1 and kind == "bool":
                        values[rule] = bool(data[pos])
                        break
                    if tag == 3 and kind == "int":
                        values[rule] = int.from_bytes(data[pos:pos + 4], "little", signed=True)
                        break
                idx = data.find(key, idx + 1)
    except Exception:
        pass
    return values

def make_executable(file_path: Path):
    """Make a file executable on Unix systems."""
    if sys.platform != "win32":
        try:
            # Set executable permissions (chmod +x)
            current_permissions = file_path.stat().st_mode
            file_path.chmod(current_permissions | 0o111)  # Add execute permission for all
        except Exception as e:
            print(f"Warning: Could not make {file_path} executable: {e}")

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Unknown"

def get_world_info(server_path: Path) -> List[Dict]:
    worlds = []
    worlds_dir = server_path / "worlds"
    if worlds_dir.exists():
        for world_dir in worlds_dir.iterdir():
            if world_dir.is_dir():
                level_dat = world_dir / "level.dat"
                world_info = {
                    "name": world_dir.name,
                    "size": format_size(get_folder_size(world_dir)),
                    "last_modified": "Unknown",
                    "version": get_world_last_opened_version(world_dir)
                }
                if level_dat.exists():
                    try:
                        mtime = datetime.fromtimestamp(level_dat.stat().st_mtime)
                        world_info["last_modified"] = mtime.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass
                worlds.append(world_info)
    return worlds

def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])

def open_url(url: str):
    webbrowser.open(url)


# ============================================================================
# BACKUP MANAGER
# ============================================================================

class BackupManager:
    def __init__(self, server_path: Path, config: dict):
        self.server_path = server_path
        self.config = config
        # New backups are namespaced per-Server (docs/V2-MAJORDOMO-PLAN.md,
        # "Config v2 & migration") so multiple Servers sharing a parent folder
        # don't mix their backups. Nothing here MOVES the pre-2.0 flat
        # backups -- list_backups()/cleanup still find them under
        # legacy_backup_dir, so rolling back to 1.0.4 still finds everything.
        self.backup_dir = server_path.parent / "bedrock_backups" / server_path.name
        self.legacy_backup_dir = server_path.parent / "bedrock_backups"

    def get_backup_dir(self) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        return self.backup_dir

    def list_backups(self) -> List[Dict]:
        backups = []
        dirs = [self.backup_dir]
        if self.legacy_backup_dir.exists() and self.legacy_backup_dir != self.backup_dir:
            dirs.append(self.legacy_backup_dir)
        for backup_dir in dirs:
            if not backup_dir.exists():
                continue
            for item in backup_dir.iterdir():
                if item.name.startswith("backup_"):
                    try:
                        size = get_folder_size(item) if item.is_dir() else item.stat().st_size
                        mtime = datetime.fromtimestamp(item.stat().st_mtime)
                        backups.append({
                            "path": item,
                            "name": item.name,
                            "size": format_size(size),
                            "date": mtime.strftime("%Y-%m-%d %H:%M:%S"),
                            "timestamp": mtime
                        })
                    except Exception:
                        pass
        backups.sort(key=lambda b: b["timestamp"], reverse=True)
        return backups
    
    def create_backup(self, preserve_items: List[str], compress: bool = False, 
                      progress_callback=None) -> Tuple[bool, Path, List[str]]:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"backup_{timestamp}"
        backup_path = self.get_backup_dir() / backup_name
        
        if compress:
            backup_path = backup_path.with_suffix('.zip')
        
        backed_up = []
        
        try:
            if compress:
                with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for i, item in enumerate(preserve_items):
                        source = self.server_path / item
                        if source.exists():
                            if source.is_dir():
                                for file in source.rglob("*"):
                                    if file.is_file():
                                        arcname = str(file.relative_to(self.server_path))
                                        zf.write(file, arcname)
                            else:
                                zf.write(source, item)
                            backed_up.append(item)
                        if progress_callback:
                            progress_callback((i + 1) / len(preserve_items) * 100)
            else:
                backup_path.mkdir(exist_ok=True)
                for i, item in enumerate(preserve_items):
                    source = self.server_path / item
                    if source.exists():
                        dest = backup_path / item
                        if source.is_dir():
                            shutil.copytree(source, dest)
                        else:
                            shutil.copy2(source, dest)
                        backed_up.append(item)
                    if progress_callback:
                        progress_callback((i + 1) / len(preserve_items) * 100)
            
            return True, backup_path, backed_up
        
        except Exception as e:
            # Cleanup failed backup
            if backup_path.exists():
                if backup_path.is_dir():
                    shutil.rmtree(backup_path)
                else:
                    backup_path.unlink()
            raise e
    
    def restore_backup(self, backup_path: Path, progress_callback=None) -> Tuple[bool, List[str]]:
        restored = []
        
        try:
            if backup_path.suffix == '.zip':
                with zipfile.ZipFile(backup_path, 'r') as zf:
                    members = zf.namelist()
                    for i, member in enumerate(members):
                        # Get top-level item name
                        top_level = member.split('/')[0]
                        if top_level not in restored:
                            dest = self.server_path / top_level
                            if dest.exists():
                                if dest.is_dir():
                                    shutil.rmtree(dest)
                                else:
                                    dest.unlink()
                            restored.append(top_level)
                        zf.extract(member, self.server_path)
                        if progress_callback and i % 50 == 0:
                            progress_callback((i + 1) / len(members) * 100)
            else:
                items = list(backup_path.iterdir())
                for i, item in enumerate(items):
                    dest = self.server_path / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    if item.is_dir():
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)
                    restored.append(item.name)
                    if progress_callback:
                        progress_callback((i + 1) / len(items) * 100)
            
            return True, restored
        
        except Exception as e:
            raise e
    
    def cleanup_old_backups(self, max_backups: int) -> int:
        backups = self.list_backups()
        deleted = 0
        
        if len(backups) > max_backups:
            for backup in backups[max_backups:]:
                try:
                    if backup["path"].is_dir():
                        shutil.rmtree(backup["path"])
                    else:
                        backup["path"].unlink()
                    deleted += 1
                except Exception:
                    pass
        
        return deleted
    
    def delete_backup(self, backup_path: Path) -> bool:
        try:
            if backup_path.is_dir():
                shutil.rmtree(backup_path)
            else:
                backup_path.unlink()
            return True
        except Exception:
            return False

# ============================================================================
# SERVER PROCESS MANAGER
# ============================================================================

class ServerManager:
    def __init__(self, server_path: Path):
        self.server_path = server_path
        self.process: Optional[subprocess.Popen] = None
        self.output_callbacks = []
        self.status_callbacks = []
        self._running = False
        self._output_thread = None
    
    def add_output_callback(self, callback):
        self.output_callbacks.append(callback)
    
    def add_status_callback(self, callback):
        self.status_callbacks.append(callback)
    
    def _notify_output(self, line: str):
        for cb in self.output_callbacks:
            try:
                cb(line)
            except Exception:
                pass
    
    def _notify_status(self, status: str):
        for cb in self.status_callbacks:
            try:
                cb(status)
            except Exception:
                pass
    
    def is_running(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None
    
    def start(self) -> bool:
        if self.is_running():
            return False
        
        executable = self.server_path / SERVER_EXECUTABLE
        if not executable.exists():
            self._notify_output(f"ERROR: Server executable not found: {executable}")
            return False
        
        try:
            # Set executable permission on Linux
            if sys.platform != "win32":
                os.chmod(executable, 0o755)
            
            # Start the server process
            if sys.platform == "win32":
                self.process = subprocess.Popen(
                    [str(executable)],
                    cwd=str(self.server_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
            else:
                # Linux/macOS - Set LD_LIBRARY_PATH for Bedrock server libraries
                env = os.environ.copy()
                env['LD_LIBRARY_PATH'] = str(self.server_path)
                
                self.process = subprocess.Popen(
                    [str(executable)],
                    cwd=str(self.server_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env
                )
            
            self._running = True
            self._notify_status("running")
            self._notify_output("Server starting...")
            
            # Start output reading thread
            self._output_thread = threading.Thread(target=self._read_output, daemon=True)
            self._output_thread.start()
            
            return True
        
        except Exception as e:
            self._notify_output(f"ERROR: Failed to start server: {str(e)}")
            return False
    
    def _read_output(self):
        try:
            while self._running and self.process:
                line = self.process.stdout.readline()
                if line:
                    self._notify_output(line.rstrip())
                elif self.process.poll() is not None:
                    break
        except Exception:
            pass
        finally:
            self._running = False
            self._notify_status("stopped")
            self._notify_output("Server stopped.")
    
    def send_command(self, command: str):
        if self.is_running() and self.process.stdin:
            try:
                self.process.stdin.write(command + "\n")
                self.process.stdin.flush()
            except Exception:
                pass
    
    def stop(self, timeout: int = 30) -> bool:
        if not self.is_running():
            return True
        
        self._notify_output("Sending stop command...")
        self.send_command("stop")
        
        # Wait for graceful shutdown
        try:
            self.process.wait(timeout=timeout)
            self._running = False
            return True
        except subprocess.TimeoutExpired:
            self._notify_output("Timeout waiting for server, forcing termination...")
            self.kill()
            return False
    
    def kill(self):
        if self.process:
            try:
                self.process.terminate()
                time.sleep(1)
                if self.process.poll() is None:
                    self.process.kill()
            except Exception:
                pass
            finally:
                self._running = False
                self._notify_status("stopped")


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class BedrockUpdaterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = load_config()
        self.is_updating = False
        self.preserve_vars = {}
        self.server_manager: Optional[ServerManager] = None
        self.backup_manager: Optional[BackupManager] = None
        # Per-profile registry (Stage 1, see docs/V2-MAJORDOMO-PLAN.md): keyed by
        # profile_id, holds {"server_manager", "backup_manager", "console_buffer"}.
        # The UI still shows one Server at a time (self.server_manager/backup_manager
        # above always alias the current profile's entry), but a running context
        # survives being deselected -- this is the foundation the sidebar (Stage 1
        # later) and simultaneous multi-Server running build on.
        self.contexts: Dict[str, dict] = {}

        self.setup_logging()
        self.setup_styles()
        self.setup_ui()
        self.apply_theme()
        self.load_saved_state()
        self.setup_keyboard_shortcuts()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Linux-specific startup message
        if sys.platform != "win32":
            self.log("Running on Linux - executable permissions will be set automatically", "info")
        
        # Check for updates on start if enabled
        if self.config.get("check_updates_on_start"):
            self.root.after(2000, self.check_for_updates_silent)
    
    def setup_logging(self):
        self.log_messages = []
        log_file = get_log_dir() / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[logging.FileHandler(log_file)]
        )
        self.logger = logging.getLogger(__name__)
        
    def log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}"
        print(formatted_msg)
        if level == "error":
            self.logger.error(message)
        elif level == "success":
            self.logger.info(f"SUCCESS: {message}")
        else:
            self.logger.info(message)
        if hasattr(self, 'status_label'):
            self.status_label.config(text=message)
            
    def setup_styles(self):
        self.style = ttk.Style()
        self.style.configure("Success.TButton", foreground="green")
        self.style.configure("Danger.TButton", foreground="red")
        self.style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"))
        # Green step arrows and compact secondary buttons for the Update tab.
        self.style.configure("Arrow.TLabel", foreground="#4CAF50", font=("TkDefaultFont", 13, "bold"))
        self.style.configure("Small.TButton", font=("TkDefaultFont", 8))

    def set_window_icon(self):
        """Show minecraft.png as the window/taskbar icon.

        Works both from source (icon sits next to this script) and from a
        PyInstaller bundle (data files are unpacked under sys._MEIPASS).
        """
        try:
            base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
            icon_path = base / "minecraft.png"
            if icon_path.exists():
                # Keep a reference so the image isn't garbage-collected.
                self._icon_image = tk.PhotoImage(file=str(icon_path))
                # default=True also applies the icon to dialogs and tooltips.
                self.root.iconphoto(True, self._icon_image)
        except Exception as e:
            print(f"Could not set window icon: {e}")

    def parse_server_properties(self, filepath: Path) -> Dict[str, str]:
        props = {}
        if filepath.is_dir():
            filepath = filepath / "server.properties"
        if not filepath.exists(): 
            return props
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        props[key.strip()] = value.strip()
        except Exception as e:
            self.log(f"Error parsing properties: {e}", "error")
        return props

    def save_server_properties(self, filepath: Path, props: Dict[str, str]):
        if not filepath.exists():
            with open(filepath, 'w') as f:
                for k, v in props.items(): f.write(f"{k}={v}\n")
            return True
        try:
            with open(filepath, 'r') as f:
                lines = f.readlines()
            used_keys = set()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith('#') and '=' in stripped:
                    key = stripped.split('=', 1)[0].strip()
                    if key in props:
                        new_lines.append(f"{key}={props[key]}\n")
                        used_keys.add(key)
                        continue
                new_lines.append(line)
            for key, value in props.items():
                if key not in used_keys:
                    new_lines.append(f"{key}={value}\n")
            with open(filepath, 'w') as f:
                f.writelines(new_lines)
            return True
        except Exception as e:
            self.log(f"Error saving properties: {e}", "error")
            return False

    def setup_keyboard_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self.browse_server())
        self.root.bind("<Control-s>", lambda e: self.manual_backup())
        self.root.bind("<Control-u>", lambda e: self.start_update())
        self.root.bind("<F5>", lambda e: self.validate_inputs())
        self.root.bind("<F1>", lambda e: self.show_about())
    
    def setup_ui(self):
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry(self.config.get("window_geometry", "900x700"))
        self.root.minsize(800, 600)
        self.set_window_icon()

        # Sidebar (Machines -> Servers) + the existing tabbed Notebook, side by
        # side in a resizable pane. See docs/V2-MAJORDOMO-PLAN.md, "GUI: sidebar
        # + the same 7 tabs" -- Stage 1 only ever has "This computer" as a
        # Machine; remote Machines arrive in Stage 3.
        self.main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        sidebar_frame = ttk.Frame(self.main_pane, width=200)
        self.main_pane.add(sidebar_frame, weight=0)
        self.setup_sidebar(sidebar_frame)

        notebook_frame = ttk.Frame(self.main_pane)
        self.main_pane.add(notebook_frame, weight=1)

        self.notebook = ttk.Notebook(notebook_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Tab order: daily use first — Server is the home tab.
        self.server_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.server_tab, text="🎮 Server")
        self.setup_server_tab()

        self.worlds_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.worlds_tab, text="🌍 Worlds")
        self.setup_worlds_tab()

        self.players_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.players_tab, text="👥 Players")
        self.setup_players_tab()

        self.properties_editor = ServerPropertiesEditor(self.notebook, self)
        self.notebook.add(self.properties_editor, text="📝 Configuration")

        self.backup_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.backup_tab, text="💾 Backups")
        self.setup_backup_tab()

        self.main_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.main_tab, text="🔄 Update")
        self.setup_main_tab()

        self.settings_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.settings_tab, text="⚙️ Settings")
        self.setup_settings_tab()

        # Auto-refresh dynamic tabs when they're opened, so lists never go stale
        # (bound after construction, so startup tab-selection can't fire it early).
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        
        self.status_bar = ttk.Frame(self.root)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        self.status_label = ttk.Label(self.status_bar, text="Ready")
        self.status_label.pack(side=tk.LEFT)
        self.server_status_label = ttk.Label(self.status_bar, text="⬤ Server: Not configured", foreground="gray")
        self.server_status_label.pack(side=tk.RIGHT)

    def setup_sidebar(self, parent):
        ttk.Label(parent, text="🖥 This computer", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w", padx=6, pady=(6, 2))
        self.sidebar_tree = ttk.Treeview(parent, show="tree", height=15)
        self.sidebar_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.sidebar_tree.bind("<<TreeviewSelect>>", self.on_sidebar_select)
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, padx=4, pady=(0, 6))
        ttk.Button(btns, text="➕ Server", command=self.add_server_profile).pack(fill=tk.X)

    def refresh_sidebar(self):
        """Rebuild the Machines/Servers tree from config + live registry state."""
        if not hasattr(self, 'sidebar_tree'):
            return
        self.sidebar_tree.delete(*self.sidebar_tree.get_children())
        machine_node = self.sidebar_tree.insert("", tk.END, iid="machine:local",
                                                 text="🖥 This computer", open=True)
        active_id = self.config.get("active_profile")
        for pid, profile in self.config.get("server_profiles", {}).items():
            running = pid in self.contexts and self.contexts[pid]["server_manager"].is_running()
            dot = "🟢" if running else "⚪"
            self.sidebar_tree.insert(machine_node, tk.END, iid=f"profile:{pid}",
                                      text=f"{dot} {profile.get('name') or 'Server'}")
        if active_id:
            try:
                self.sidebar_tree.selection_set(f"profile:{active_id}")
            except tk.TclError:
                pass

    def on_sidebar_select(self, event=None):
        sel = self.sidebar_tree.selection()
        if not sel:
            return
        iid = sel[0]
        active_id = self.config.get("active_profile")
        if not iid.startswith("profile:"):
            # The Machine node itself -- no Machine/Fleet page yet (Stage 4);
            # keep showing whichever Server is already active.
            if active_id:
                self.sidebar_tree.selection_set(f"profile:{active_id}")
            return
        new_id = iid.split(":", 1)[1]
        if new_id == active_id:
            return
        if hasattr(self, 'properties_editor') and self.properties_editor.has_unsaved_changes():
            if not messagebox.askyesno("Unsaved changes",
                    "The Active Server Configuration has unsaved changes.\n\n"
                    "Discard them and switch Servers?"):
                if active_id:
                    self.sidebar_tree.selection_set(f"profile:{active_id}")
                return
        self._switch_to_profile(new_id)

    def _switch_to_profile(self, profile_id: str):
        """Point every tab at a different Server. See docs/V2-MAJORDOMO-PLAN.md,
        'GUI: sidebar + the same 7 tabs' -- a still-running previous Server is
        NOT stopped; it keeps running in the registry, which is the point."""
        self._sync_flat_settings_into_active_profile()
        self.config["active_profile"] = profile_id
        hydrate_active_profile_cache(self.config)
        self.server_entry.delete(0, tk.END)
        self.server_entry.insert(0, self.config.get("last_server_path", ""))
        for item, var in self.preserve_vars.items():
            if item in self.config["preserve_items"]:
                var.set(self.config["preserve_items"][item].get("enabled", True))
        if hasattr(self, 'max_backups_var'):
            self.max_backups_var.set(self.config.get("max_backups", 5))
            self.compress_var.set(self.config.get("compress_backups", False))
            self.auto_cleanup_var.set(self.config.get("auto_cleanup_backups", True))
            self.auto_stop_var.set(self.config.get("auto_stop_server_before_update", True))
            self.auto_start_var.set(self.config.get("auto_start_server_after_update", False))
        self.initialize_managers()
        self.validate_inputs()
        self.refresh_sidebar()

    def add_server_profile(self):
        """The sidebar's [+ Server]: adds a genuinely new, independent profile
        (unlike Settings > Browse, which relocates the CURRENT Server)."""
        folderpath = filedialog.askdirectory(initialdir=str(Path.home()), title="Select Bedrock Server Folder")
        if not folderpath:
            return
        if hasattr(self, 'properties_editor') and self.properties_editor.has_unsaved_changes():
            if not messagebox.askyesno("Unsaved changes",
                    "The Active Server Configuration has unsaved changes.\n\n"
                    "Discard them and add a new Server?"):
                return
        self._sync_flat_settings_into_active_profile()
        profile_id = uuid.uuid4().hex[:8]
        name = _peek_server_name(Path(folderpath)) or Path(folderpath).name or "Server"
        self.config.setdefault("server_profiles", {})[profile_id] = {
            "name": name,
            "path": folderpath,
            "preserve_items": copy.deepcopy(DEFAULT_PRESERVE_ITEMS),
            "max_backups": 5,
            "compress_backups": False,
            "auto_cleanup_backups": True,
            "auto_stop_server_before_update": True,
            "auto_start_server_after_update": False,
            "known_players": {},
        }
        self._switch_to_profile(profile_id)

    def setup_main_tab(self):
        self.main_tab.columnconfigure(0, weight=1)
        
        header = ttk.Frame(self.main_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Update Bedrock Server", font=("TkDefaultFont", 14, "bold")).pack(side=tk.LEFT)
        file_frame = ttk.LabelFrame(self.main_tab, text="File Selection", padding=10)
        file_frame.grid(row=1, column=0, sticky="ew", pady=5)
        file_frame.columnconfigure(1, weight=1)
        
        ttk.Label(file_frame, text="New Bedrock Server Version (ZIP):").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.zip_entry = ttk.Entry(file_frame)
        self.zip_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.zip_entry.bind("<KeyRelease>", self.validate_inputs)
        ttk.Button(file_frame, text="Browse", command=self.browse_zip).grid(row=0, column=2, padx=5)
        self.zip_status = ttk.Label(file_frame, text="", foreground="gray")
        self.zip_status.grid(row=1, column=1, sticky="w", padx=5)
        
        self.update_installed_label = ttk.Label(file_frame, text="Installed Bedrock Server Version: —", foreground="gray")
        self.update_installed_label.grid(row=2, column=1, sticky="w", padx=5, pady=(4, 0))
        
        progress_frame = ttk.Frame(self.main_tab)
        progress_frame.grid(row=4, column=0, sticky="ew", pady=10)
        progress_frame.columnconfigure(0, weight=1)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky="ew", pady=5)
        self.progress_label = ttk.Label(progress_frame, text="Ready")
        self.progress_label.grid(row=1, column=0, sticky="w")
        
        button_frame = ttk.Frame(self.main_tab)
        button_frame.grid(row=5, column=0, sticky="ew", pady=5)

        # Normal update procedure, laid out as numbered steps 1 -> 2 -> 3 (left).
        steps = ttk.Frame(button_frame)
        steps.pack(side=tk.LEFT)
        ttk.Label(steps, text="To update:", font=("TkDefaultFont", 9, "bold")).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(steps, text="1:  🌐 Wiki Version", command=self.manual_version_input).pack(side=tk.LEFT)
        ttk.Label(steps, text="→", style="Arrow.TLabel").pack(side=tk.LEFT, padx=5)
        ttk.Button(steps, text="2:  ⬇️ Download Latest", command=self.open_download_page).pack(side=tk.LEFT)
        ttk.Label(steps, text="→", style="Arrow.TLabel").pack(side=tk.LEFT, padx=5)
        self.update_button = ttk.Button(steps, text="3:  🚀 Update Server", command=self.start_update, style="Primary.TButton")
        self.update_button.pack(side=tk.LEFT)

        # Secondary, occasional actions: smaller and tucked to the right.
        extras = ttk.Frame(button_frame)
        extras.pack(side=tk.RIGHT)
        ttk.Button(extras, text="📂 Open Folder", command=self.open_server_folder, style="Small.TButton").pack(side=tk.RIGHT, padx=2)
        ttk.Button(extras, text="📋 Dry Run", command=self.dry_run, style="Small.TButton").pack(side=tk.RIGHT, padx=2)
        
        log_frame = ttk.LabelFrame(self.main_tab, text="Activity Log", padding=5)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=5)
        self.main_tab.rowconfigure(6, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED,
                               font=("Consolas" if sys.platform == "win32" else "Monaco", self.config.get("console_font_size", 9)))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.config(command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        self.log_text.tag_config("info", foreground="#2196F3")
        self.log_text.tag_config("success", foreground="#4CAF50")
        self.log_text.tag_config("warning", foreground="#FF9800")
        self.log_text.tag_config("error", foreground="#F44336")
        self.log("Application started. (First time? Set your Server Folder in ⚙️ Settings.)", "info")
    
    def setup_server_tab(self):
        self.server_tab.columnconfigure(0, weight=1)
        self.server_tab.rowconfigure(2, weight=1)
        info_frame = ttk.LabelFrame(self.server_tab, text="Active Server Information", padding=10)
        info_frame.grid(row=0, column=0, sticky="ew", pady=5)
        self.info_text = ttk.Label(info_frame, text="No Server selected — set the Server Folder in ⚙️ Settings.")
        self.info_text.pack(anchor="w")
        control_frame = ttk.LabelFrame(self.server_tab, text="Server Control", padding=10)
        control_frame.grid(row=1, column=0, sticky="ew", pady=5)
        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(fill=tk.X)
        self.start_btn = ttk.Button(btn_frame, text="▶️ Start", command=self.start_server, width=15)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = ttk.Button(btn_frame, text="⏹️ Stop", command=self.stop_server, width=15)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.restart_btn = ttk.Button(btn_frame, text="🔄 Restart", command=self.restart_server, width=15)
        self.restart_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🎲 Gamerules", command=self.open_gamerules_dialog, width=15).pack(side=tk.LEFT, padx=5)
        self.server_running_label = ttk.Label(btn_frame, text="⬤ Stopped", foreground="red")
        self.server_running_label.pack(side=tk.RIGHT, padx=20)
        world_frame = ttk.Frame(control_frame)
        world_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(world_frame, text="Active World:").pack(side=tk.LEFT)
        self.world_combo = ttk.Combobox(world_frame, state="readonly", width=32)
        self.world_combo.pack(side=tk.LEFT, padx=8)
        self.world_combo.bind("<<ComboboxSelected>>", self.on_world_selected)
        # Empty while stopped; explains the greyed-out dropdown while the Server runs.
        self.world_hint_label = ttk.Label(world_frame, text="", font=("TkDefaultFont", 8), foreground="#FF9800")
        self.world_hint_label.pack(side=tk.LEFT)
        net_frame = ttk.Frame(control_frame)
        net_frame.pack(fill=tk.X, pady=(10, 0))
        self.network_label = ttk.Label(net_frame, text="Network: Not configured")
        self.network_label.pack(side=tk.LEFT)
        ttk.Button(net_frame, text="📋 Copy IP", command=self.copy_server_ip).pack(side=tk.RIGHT)
        console_frame = ttk.LabelFrame(self.server_tab, text="Server Console", padding=5)
        console_frame.grid(row=2, column=0, sticky="nsew", pady=5)
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)
        console_scroll = ttk.Scrollbar(console_frame)
        console_scroll.grid(row=0, column=1, sticky="ns")
        self.console_text = tk.Text(console_frame, height=10, wrap=tk.WORD, state=tk.DISABLED,
                                   font=("Consolas" if sys.platform == "win32" else "Monaco", self.config.get("console_font_size", 9)),
                                   bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff")
        self.console_text.grid(row=0, column=0, sticky="nsew")
        console_scroll.config(command=self.console_text.yview)
        self.console_text.config(yscrollcommand=console_scroll.set)
        cmd_frame = ttk.Frame(console_frame)
        cmd_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        cmd_frame.columnconfigure(0, weight=1)
        self.cmd_entry = ttk.Entry(cmd_frame)
        self.cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.cmd_entry.bind("<Return>", self.send_server_command)
        ttk.Button(cmd_frame, text="Send", command=self.send_server_command).grid(row=0, column=1)
        quick_frame = ttk.Frame(console_frame)
        quick_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(quick_frame, text="Quick:").pack(side=tk.LEFT, padx=(0, 5))
        for cmd in ["list", "save hold", "save query", "save resume", "say Hello!"]:
            ttk.Button(quick_frame, text=cmd, command=lambda c=cmd: self.quick_command(c)).pack(side=tk.LEFT, padx=2)
    
    def setup_backup_tab(self):
        self.backup_tab.columnconfigure(0, weight=1)
        self.backup_tab.rowconfigure(3, weight=1)
        self.backup_header_label = ttk.Label(self.backup_tab, text="Backups for: (no Server selected)",
                                             font=("TkDefaultFont", 10, "bold"))
        self.backup_header_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        control_frame = ttk.Frame(self.backup_tab)
        control_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(control_frame, text="💾 Create Backup Now", command=self.manual_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="🧹 Cleanup Old Backups", command=self.cleanup_backups).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="📂 Open Folder", command=self.open_backup_folder).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="🔄 Refresh", command=self.refresh_backups).pack(side=tk.RIGHT, padx=5)
        preserve_frame = ttk.LabelFrame(self.backup_tab, text="What to back up (also what updates preserve)", padding=10)
        preserve_frame.grid(row=2, column=0, sticky="ew", pady=5)
        canvas = tk.Canvas(preserve_frame, height=120)
        scrollbar = ttk.Scrollbar(preserve_frame, orient="vertical", command=canvas.yview)
        self.preserve_inner = ttk.Frame(canvas)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        canvas.create_window((0, 0), window=self.preserve_inner, anchor="nw")
        self.preserve_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        col, row = 0, 0
        for item, props in self.config["preserve_items"].items():
            var = tk.BooleanVar(value=props["enabled"])
            self.preserve_vars[item] = var
            text = f"⭐ {item}" if props.get("critical") else item
            cb = ttk.Checkbutton(self.preserve_inner, text=text, variable=var)
            cb.grid(row=row, column=col, sticky="w", padx=10, pady=2)
            self.create_tooltip(cb, props["description"])
            col += 1
            if col >= 3:
                col = 0
                row += 1
        list_frame = ttk.LabelFrame(self.backup_tab, text="Available Backups", padding=10)
        list_frame.grid(row=3, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("name", "date", "size")
        self.backup_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=5)
        self.backup_tree.heading("name", text="Backup Name")
        self.backup_tree.heading("date", text="Date Created")
        self.backup_tree.heading("size", text="Size")
        self.backup_tree.column("name", width=300)
        self.backup_tree.column("date", width=150)
        self.backup_tree.column("size", width=100)
        backup_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.backup_tree.yview)
        self.backup_tree.configure(yscrollcommand=backup_scroll.set)
        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        backup_scroll.grid(row=0, column=1, sticky="ns")
        action_frame = ttk.Frame(self.backup_tab)
        action_frame.grid(row=4, column=0, sticky="ew", pady=10)
        ttk.Button(action_frame, text="🔄 Restore Selected", command=self.restore_selected_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="❌ Delete Selected", command=self.delete_selected_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="📂 Open in Explorer", command=self.open_selected_backup).pack(side=tk.LEFT, padx=5)
    
    def setup_worlds_tab(self):
        self.worlds_tab.columnconfigure(0, weight=1)
        self.worlds_tab.rowconfigure(1, weight=1)
        control_frame = ttk.Frame(self.worlds_tab)
        control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Button(control_frame, text="✨ Create New World", command=self.create_new_world,
                   style="Primary.TButton").pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="🔄 Refresh", command=self.refresh_worlds).pack(side=tk.RIGHT, padx=5)
        ttk.Button(control_frame, text="📂 Open Worlds Folder", command=self.open_worlds_folder).pack(side=tk.RIGHT, padx=5)
        list_frame = ttk.LabelFrame(self.worlds_tab, text="Worlds on this Server", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        columns = ("name", "size", "last_modified", "version")
        self.world_tree = ttk.Treeview(list_frame, columns=columns, show="tree headings", height=10)
        self.world_tree.heading("#0", text="")
        self.world_tree.column("#0", width=90, anchor="center", stretch=False)
        self.world_tree.heading("name", text="World Name")
        self.world_tree.heading("size", text="Size")
        self.world_tree.heading("last_modified", text="Last Modified")
        self.world_tree.heading("version", text="Last Run On")
        self.world_tree.column("name", width=240)
        self.world_tree.column("size", width=90)
        self.world_tree.column("last_modified", width=140)
        self.world_tree.column("version", width=140)
        world_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.world_tree.yview)
        self.world_tree.configure(yscrollcommand=world_scroll.set)
        self.world_tree.grid(row=0, column=0, sticky="nsew")
        world_scroll.grid(row=0, column=1, sticky="ns")
        self.world_tree.bind("<<TreeviewSelect>>", self.on_world_select)
        action_frame = ttk.Frame(self.worlds_tab)
        action_frame.grid(row=2, column=0, sticky="ew", pady=10)
        ttk.Button(action_frame, text="🎯 Set as Active World", command=self.set_selected_world_active).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="✏️ Rename", command=self.rename_selected_world).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="🗑️ Delete", command=self.delete_selected_world).pack(side=tk.LEFT, padx=5)
        self.world_info_label = ttk.Label(
            self.worlds_tab,
            text="A World's save is upgraded by the version that runs it — it won't load on an older Bedrock Server Version.",
            font=("TkDefaultFont", 8), foreground="gray")
        self.world_info_label.grid(row=3, column=0, sticky="w")
    
    def refresh_world_combo(self):
        """Fill the Active World dropdown from the Server's worlds folder."""
        if not hasattr(self, 'world_combo'):
            return
        server_path = self.server_entry.get()
        if not server_path or not Path(server_path).exists():
            self.world_combo.config(values=[])
            self.world_combo.set("")
            return
        worlds = [w["name"] for w in get_world_info(Path(server_path))]
        props = self.parse_server_properties(Path(server_path) / "server.properties")
        current = props.get("level-name", "")
        if current and current not in worlds:
            worlds = worlds + [current]  # created but not generated yet
        # Plain world names; the dropdown's selected value IS the active one
        # (the "Active World:" label already says what it is).
        self.world_combo.config(values=worlds)
        self.world_combo.set(current)

    def set_active_world(self, new_name: str) -> bool:
        """Point level-name at a world folder; takes effect on next Server start."""
        server_path = self.server_entry.get()
        if not server_path or not new_name:
            return False
        props_path = Path(server_path) / "server.properties"
        props = self.parse_server_properties(props_path)
        props["level-name"] = new_name
        if self.save_server_properties(props_path, props):
            self.log(f"Active World set to: {new_name} (takes effect on next start)", "success")
            if hasattr(self, 'properties_editor'):
                self.properties_editor.load_properties()
            self.update_server_info()
            return True
        messagebox.showerror("Error", "Could not update the Active World.")
        return False

    def on_world_selected(self, event=None):
        new_name = self.world_combo.get()
        if not new_name:
            return
        if self.server_manager and self.server_manager.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before switching the Active World.")
            self.refresh_world_combo()
            return
        self.set_active_world(new_name)

    def set_selected_world_active(self):
        selected = self.world_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No World selected.")
            return
        name = str(self.world_tree.item(selected[0])["values"][0])
        server_path = self.server_entry.get()
        if not server_path:
            return
        props = self.parse_server_properties(Path(server_path) / "server.properties")
        if name == props.get("level-name"):
            self.log(f"'{name}' is already the Active World", "info")
            return
        if self.server_manager and self.server_manager.is_running():
            # Explicit switch action: confirm, then stop nicely -> switch -> start again.
            if not messagebox.askyesno("Switch World",
                    f"The Server is running.\n\nStop it nicely, switch to '{name}', and start it again?"):
                return
            self.log(f"Switching to '{name}': stopping the running Server...", "info")
            def do_switch():
                self.server_manager.stop()
                self.root.after(0, lambda: self._finish_world_switch(name))
            threading.Thread(target=do_switch, daemon=True).start()
        else:
            if self.set_active_world(name):
                self.refresh_world_combo()
                self.refresh_worlds()
                messagebox.showinfo("Active World",
                    f"'{name}' is now the Active World.\nStart the Server to load it.")

    def _finish_world_switch(self, name: str):
        """Runs on the UI thread after the Server stopped: switch world, start again."""
        if self.set_active_world(name):
            self.refresh_world_combo()
            self.refresh_worlds()
            self.log(f"Starting the Server on '{name}'...", "info")
            self.start_server()

    def create_new_world(self):
        server_path = self.server_entry.get()
        if not server_path or not Path(server_path).exists():
            messagebox.showwarning("Warning", "No Server configured. Set the Server Folder in ⚙️ Settings first.")
            return
        if self.server_manager and self.server_manager.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before creating a new World.")
            return
        name = simpledialog.askstring("Create New World", "Name of the new World:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name or any(c in name for c in '/\\:*?"<>|'):
            messagebox.showerror("Invalid Name", "The World name is empty or contains invalid characters.")
            return
        existing = [w["name"] for w in get_world_info(Path(server_path))]
        if name in existing:
            if not messagebox.askyesno("World Exists", f"'{name}' already exists.\n\nSet it as the Active World instead?"):
                return
        if self.set_active_world(name):
            self.refresh_world_combo()
            self.refresh_worlds()
            if name not in existing:
                # Soft link: offer to name the Server after the World, but only while it
                # still carries the stock name (never overwrite a name the user chose).
                props = self.parse_server_properties(Path(server_path) / "server.properties")
                if props.get("server-name", "").strip() in ("", "Dedicated Server"):
                    if messagebox.askyesno("Name the Server too?",
                            "Your Server still has the stock name 'Dedicated Server' — that's the name\n"
                            "players see in their server list when they connect.\n\n"
                            f"Name the Server '{name}' as well?"):
                        props["server-name"] = name
                        self.save_server_properties(Path(server_path) / "server.properties", props)
                        self.update_server_info()
                messagebox.showinfo("World Created",
                    f"'{name}' is now the Active World.\n\n"
                    "Bedrock will generate it the first time you start the Server.\n"
                    "Taking you to 📝 Configuration — seed, gamemode and\n"
                    "difficulty shape the new World on its first start.")
                self.notebook.select(self.properties_editor)
                self.properties_editor.load_properties()

    def rename_selected_world(self):
        selected = self.world_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No World selected.")
            return
        old_name = str(self.world_tree.item(selected[0])["values"][0])
        server_path = self.server_entry.get()
        if not server_path:
            return
        if self.server_manager and self.server_manager.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before renaming a World.")
            return
        new_name = simpledialog.askstring("Rename World", f"New name for '{old_name}':", parent=self.root)
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or any(c in new_name for c in '/\\:*?"<>|'):
            messagebox.showerror("Invalid Name", "The World name is empty or contains invalid characters.")
            return
        worlds_dir = Path(server_path) / "worlds"
        if not (worlds_dir / old_name).exists():
            messagebox.showinfo("Not created yet",
                f"'{old_name}' hasn't been generated yet — it's only the Active World pointer.\n"
                "Start the Server once to create it, or just create a new World with the name you want.")
            return
        if (worlds_dir / new_name).exists():
            messagebox.showerror("Exists", f"A World named '{new_name}' already exists.")
            return
        try:
            (worlds_dir / old_name).rename(worlds_dir / new_name)
        except Exception as e:
            messagebox.showerror("Error", f"Could not rename World:\n{e}")
            return
        props = self.parse_server_properties(Path(server_path) / "server.properties")
        if props.get("level-name") == old_name:
            self.set_active_world(new_name)
        self.log(f"Renamed World '{old_name}' to '{new_name}'", "success")
        self.refresh_worlds()
        self.refresh_world_combo()

    def delete_selected_world(self):
        selected = self.world_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No World selected.")
            return
        name = str(self.world_tree.item(selected[0])["values"][0])
        server_path = self.server_entry.get()
        if not server_path:
            return
        props = self.parse_server_properties(Path(server_path) / "server.properties")
        if name == props.get("level-name"):
            messagebox.showwarning("Active World", "You can't delete the Active World. Switch to another World first.")
            return
        if self.server_manager and self.server_manager.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before deleting a World.")
            return
        if not messagebox.askyesno("Delete World",
                f"Permanently delete the World '{name}'?\n\nThis cannot be undone (older backups may still contain it)."):
            return
        try:
            shutil.rmtree(Path(server_path) / "worlds" / name)
            self.log(f"Deleted World: {name}", "info")
        except Exception as e:
            messagebox.showerror("Error", f"Could not delete World:\n{e}")
        self.refresh_worlds()
        self.refresh_world_combo()
        self.update_server_info()

    def setup_players_tab(self):
        self.players_tab.columnconfigure(0, weight=1)
        # --- Access: the allowlist (Bedrock's only join control — there is no blacklist) ---
        access = ttk.LabelFrame(self.players_tab, text="Access — who may join (allowlist.json)", padding=10)
        access.grid(row=0, column=0, sticky="ew", pady=5)
        access.columnconfigure(0, weight=1)
        self.allowlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(access,
                        text="Restrict joining to this list (Bedrock has no blacklist — leaving someone off the list is how you keep them out)",
                        variable=self.allowlist_var, command=self.toggle_allowlist_enforcement).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 5))
        acols = ("name", "xuid", "ignores_limit")
        self.allow_tree = ttk.Treeview(access, columns=acols, show="headings", height=4)
        self.allow_tree.heading("name", text="Player")
        self.allow_tree.heading("xuid", text="XUID")
        self.allow_tree.heading("ignores_limit", text="Ignores player limit")
        self.allow_tree.column("name", width=210)
        self.allow_tree.column("xuid", width=170)
        self.allow_tree.column("ignores_limit", width=130, anchor="center")
        self.allow_tree.grid(row=1, column=0, sticky="ew")
        abtns = ttk.Frame(access)
        abtns.grid(row=1, column=1, sticky="ns", padx=(8, 0))
        ttk.Button(abtns, text="➕ Add", command=self.add_allowlist_player, width=10).pack(pady=2)
        ttk.Button(abtns, text="➖ Remove", command=self.remove_allowlist_player, width=10).pack(pady=2)
        # --- Roles: permissions.json ---
        perms = ttk.LabelFrame(self.players_tab, text="Roles — visitor / member / operator (permissions.json)", padding=10)
        perms.grid(row=1, column=0, sticky="ew", pady=5)
        perms.columnconfigure(0, weight=1)
        pcols = ("name", "xuid", "permission")
        self.perm_tree = ttk.Treeview(perms, columns=pcols, show="headings", height=4)
        self.perm_tree.heading("name", text="Player")
        self.perm_tree.heading("xuid", text="XUID")
        self.perm_tree.heading("permission", text="Role")
        self.perm_tree.column("name", width=210)
        self.perm_tree.column("xuid", width=170)
        self.perm_tree.column("permission", width=100, anchor="center")
        self.perm_tree.grid(row=0, column=0, columnspan=6, sticky="ew")
        ttk.Label(perms, text="Player:").grid(row=1, column=0, sticky="e", pady=(6, 0))
        self.perm_player_combo = ttk.Combobox(perms, width=22)
        self.perm_player_combo.grid(row=1, column=1, sticky="w", padx=4, pady=(6, 0))
        ttk.Button(perms, text="Operator", command=lambda: self.set_player_permission("operator")).grid(row=1, column=2, padx=2, pady=(6, 0))
        ttk.Button(perms, text="Member", command=lambda: self.set_player_permission("member")).grid(row=1, column=3, padx=2, pady=(6, 0))
        ttk.Button(perms, text="Visitor", command=lambda: self.set_player_permission("visitor")).grid(row=1, column=4, padx=2, pady=(6, 0))
        ttk.Button(perms, text="Remove entry", command=self.remove_permission_entry).grid(row=1, column=5, padx=(10, 0), pady=(6, 0))
        # --- Per-player game mode: mixed survival/creative on one Server ---
        gm = ttk.LabelFrame(self.players_tab, text="Game mode — per player (mix survival and creative on one Server)", padding=10)
        gm.grid(row=2, column=0, sticky="ew", pady=5)
        ttk.Label(gm, text="Player:").grid(row=0, column=0, sticky="e")
        self.gm_player_combo = ttk.Combobox(gm, width=22)
        self.gm_player_combo.grid(row=0, column=1, sticky="w", padx=4)
        ttk.Button(gm, text="⛏️ Survival", command=lambda: self.set_player_gamemode("survival")).grid(row=0, column=2, padx=2)
        ttk.Button(gm, text="🧱 Creative", command=lambda: self.set_player_gamemode("creative")).grid(row=0, column=3, padx=2)
        ttk.Button(gm, text="🗺️ Adventure", command=lambda: self.set_player_gamemode("adventure")).grid(row=0, column=4, padx=2)
        ttk.Label(gm, text="Applies live to an online player and sticks for them afterwards (Server must be running).",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.gm_force_warn = ttk.Label(gm, text="", font=("TkDefaultFont", 8), foreground="#F44336")
        self.gm_force_warn.grid(row=2, column=0, columnspan=5, sticky="w")
        foot = ttk.Frame(self.players_tab)
        foot.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(foot, text="🔍 Scan console for players", command=self.scan_console_for_players).pack(side=tk.LEFT)
        ttk.Label(foot,
                  text="Names + XUIDs are learned from join lines automatically; the scan re-reads the whole console if one was missed.",
                  font=("TkDefaultFont", 8), foreground="gray").pack(side=tk.LEFT, padx=8)

    def _player_json_path(self, filename: str) -> Optional[Path]:
        server_path = self.server_entry.get()
        if not server_path or not Path(server_path).exists():
            return None
        return Path(server_path) / filename

    def _load_player_json(self, filename: str) -> list:
        p = self._player_json_path(filename)
        try:
            if p and p.exists():
                data = json.loads(p.read_text())
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _save_player_json(self, filename: str, entries: list):
        p = self._player_json_path(filename)
        if not p:
            return
        try:
            p.write_text(json.dumps(entries, indent=2))
        except Exception as e:
            messagebox.showerror("Players", f"Could not save {filename}:\n{e}")

    def refresh_players_tab(self):
        if not hasattr(self, 'allow_tree') or not hasattr(self, 'server_entry'):
            return
        server_path = self.server_entry.get()
        props = {}
        if server_path and Path(server_path).exists():
            props = self.parse_server_properties(Path(server_path))
        self.allowlist_var.set(props.get("allow-list", "false").lower() == "true")
        for i in self.allow_tree.get_children():
            self.allow_tree.delete(i)
        allow_entries = self._load_player_json("allowlist.json")
        for e in allow_entries:
            self.allow_tree.insert("", tk.END, values=(e.get("name", ""), e.get("xuid", ""),
                                                       "yes" if e.get("ignoresPlayerLimit") else "no"))
        known = self.config.get("known_players", {})
        by_xuid = {str(v): k for k, v in known.items()}
        for i in self.perm_tree.get_children():
            self.perm_tree.delete(i)
        for e in self._load_player_json("permissions.json"):
            x = str(e.get("xuid", ""))
            self.perm_tree.insert("", tk.END, values=(by_xuid.get(x, "(unknown)"), x, e.get("permission", "member")))
        names = sorted({n for n in list(known.keys()) + [a.get("name", "") for a in allow_entries] if n}, key=str.lower)
        self.perm_player_combo.config(values=names)
        self.gm_player_combo.config(values=names)
        fg = props.get("force-gamemode", "false").lower() == "true"
        self.gm_force_warn.config(text=("⚠ force-gamemode=true resets everyone to the default mode on every join — "
                                        "set it to false in 📝 Configuration to allow mixed modes." if fg else ""))

    def _scan_console_line(self, line: str):
        """Learn name<->XUID pairs live from console output, for the Players tab."""
        if self._learn_players_from_text(line):
            self.refresh_players_tab()

    def _learn_players_from_text(self, text: str) -> int:
        """Harvest all name<->XUID pairs from text. Returns the number of changes.

        Also backfills the XUID into allowlist entries that carry the name but
        no XUID (e.g. added manually before the player's XUID was known).
        """
        pairs = {}
        for m in PLAYER_XUID_RE.finditer(text):
            name = m.group(1).strip()
            if name:
                pairs[name] = m.group(2)
        if not pairs:
            return 0
        known = self.config.setdefault("known_players", {})
        changed = 0
        for name, xuid in pairs.items():
            if known.get(name) != xuid:
                known[name] = xuid
                changed += 1
        by_lower = {n.lower(): x for n, x in pairs.items()}
        entries = self._load_player_json("allowlist.json")
        backfilled = False
        for e in entries:
            nm = e.get("name", "").lower()
            if nm in by_lower and not e.get("xuid"):
                e["xuid"] = by_lower[nm]
                backfilled = True
        if backfilled:
            self._save_player_json("allowlist.json", entries)
            changed += 1
        if changed:
            save_config(self.config)
        return changed

    def scan_console_for_players(self):
        """Re-read the whole console buffer for player names/XUIDs (catches missed lines)."""
        found = self._learn_players_from_text(self.console_text.get("1.0", tk.END))
        self.refresh_players_tab()
        known = self.config.get("known_players", {})
        if found:
            self.log(f"Console scan: learned/updated {found} player entr{'y' if found == 1 else 'ies'}", "success")
        messagebox.showinfo("Scan console",
            f"Known players: {len(known)}"
            + (f"\nNew/updated in this scan: {found}" if found
               else "\nNo player join lines found in the console buffer.\n\n"
                    "Names appear when someone joins while the Server runs in this app."))

    def toggle_allowlist_enforcement(self):
        server_path = self.server_entry.get()
        if not server_path or not Path(server_path).exists():
            self.allowlist_var.set(False)
            messagebox.showwarning("Players", "No Server configured. Set the Server Folder in ⚙️ Settings first.")
            return
        enable = self.allowlist_var.get()
        if enable and not self._load_player_json("allowlist.json"):
            if not messagebox.askyesno("Empty allowlist",
                    "The allowlist is empty — with enforcement ON, nobody can join until you add players.\n\nTurn it on anyway?"):
                self.allowlist_var.set(False)
                return
        props_path = Path(server_path) / "server.properties"
        props = self.parse_server_properties(props_path)
        props["allow-list"] = "true" if enable else "false"
        self.save_server_properties(props_path, props)
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.send_command("allowlist " + ("on" if enable else "off"))
        self.log(f"Allowlist enforcement {'ON' if enable else 'OFF'}", "success")

    def add_allowlist_player(self):
        if not self._player_json_path("allowlist.json"):
            messagebox.showwarning("Players", "No Server configured. Set the Server Folder in ⚙️ Settings first.")
            return
        name = simpledialog.askstring("Add player", "Player gamertag (exact Xbox name):", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        entries = self._load_player_json("allowlist.json")
        if any(e.get("name", "").lower() == name.lower() for e in entries):
            messagebox.showinfo("Already listed", f"'{name}' is already on the allowlist.")
            return
        entry = {"ignoresPlayerLimit": False, "name": name}
        xuid = self.config.get("known_players", {}).get(name)
        if xuid:
            entry["xuid"] = str(xuid)
        entries.append(entry)
        self._save_player_json("allowlist.json", entries)
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.send_command(f'allowlist add "{name}"')
        self.log(f"Added '{name}' to the allowlist", "success")
        self.refresh_players_tab()

    def remove_allowlist_player(self):
        sel = self.allow_tree.selection()
        if not sel:
            messagebox.showwarning("Players", "Select an allowlist entry to remove.")
            return
        name = str(self.allow_tree.item(sel[0])["values"][0])
        entries = [e for e in self._load_player_json("allowlist.json") if e.get("name") != name]
        self._save_player_json("allowlist.json", entries)
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.send_command(f'allowlist remove "{name}"')
        self.log(f"Removed '{name}' from the allowlist", "info")
        self.refresh_players_tab()

    def set_player_permission(self, level: str):
        if not self._player_json_path("permissions.json"):
            messagebox.showwarning("Players", "No Server configured. Set the Server Folder in ⚙️ Settings first.")
            return
        name = self.perm_player_combo.get().strip()
        if not name:
            messagebox.showwarning("Players", "Pick or type a player name first.")
            return
        xuid = self.config.get("known_players", {}).get(name)
        if not xuid:
            xuid = simpledialog.askstring("XUID needed",
                f"No XUID known for '{name}' yet — Bedrock keys roles by XUID.\n"
                "Easiest: Cancel, and have them join once while this app runs.\n"
                "Or enter their XUID manually:", parent=self.root)
            if not xuid:
                return
            xuid = xuid.strip()
            if not xuid.isdigit():
                messagebox.showerror("Players", "A XUID is a number.")
                return
            self.config.setdefault("known_players", {})[name] = xuid
            save_config(self.config)
        entries = [e for e in self._load_player_json("permissions.json") if str(e.get("xuid")) != str(xuid)]
        entries.append({"permission": level, "xuid": str(xuid)})
        self._save_player_json("permissions.json", entries)
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.send_command("permission reload")
        self.log(f"Role for '{name}' set to {level}", "success")
        self.refresh_players_tab()

    def remove_permission_entry(self):
        sel = self.perm_tree.selection()
        if not sel:
            messagebox.showwarning("Players", "Select a role entry to remove.")
            return
        xuid = str(self.perm_tree.item(sel[0])["values"][1])
        entries = [e for e in self._load_player_json("permissions.json") if str(e.get("xuid")) != xuid]
        self._save_player_json("permissions.json", entries)
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.send_command("permission reload")
        self.log("Removed role entry (player is back at the default level)", "info")
        self.refresh_players_tab()

    def set_player_gamemode(self, mode: str):
        if not (self.server_manager and self.server_manager.is_running()):
            messagebox.showwarning("Server stopped",
                "Start the Server first — game mode is set live, per player, while they're online.")
            return
        name = self.gm_player_combo.get().strip()
        if not name:
            messagebox.showwarning("Players", "Pick or type a player name first.")
            return
        self.server_manager.send_command(f'gamemode {mode} "{name}"')
        self.console_log(f'> gamemode {mode} "{name}"')
        self.log(f"Requested {mode} for '{name}' — they must be online (check the console reply)", "info")

    def open_gamerules_dialog(self):
        server_path = self.server_entry.get()
        if not server_path or not Path(server_path).exists():
            messagebox.showwarning("Gamerules", "No Server configured. Set the Server Folder in ⚙️ Settings first.")
            return
        props = self.parse_server_properties(Path(server_path))
        active = props.get("level-name", "")
        current = read_world_gamerules(Path(server_path) / "worlds" / active)
        running = bool(self.server_manager and self.server_manager.is_running())
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Gamerules — {active}")
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=f"Gamerules for Active World: {active}",
                  font=("TkDefaultFont", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        note = ("Changes are sent live to the running Server." if running
                else "Server is stopped — values shown are from the last save. Start the Server to change them.")
        ttk.Label(frm, text=note, font=("TkDefaultFont", 8),
                  foreground=("#4CAF50" if running else "#FF9800")).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        state = tk.NORMAL if running else tk.DISABLED
        r = 2
        for rule, (kind, default) in COMMON_GAMERULES.items():
            ttk.Label(frm, text=rule).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=2)
            if kind == "bool":
                var = tk.BooleanVar(value=bool(current.get(rule, default)))
                ttk.Checkbutton(frm, variable=var, state=state,
                                command=lambda rl=rule, v=var, k=kind: self._send_gamerule(rl, v, k)).grid(row=r, column=1, sticky="w")
            else:
                var = tk.IntVar(value=int(current.get(rule, default)))
                ttk.Spinbox(frm, from_=0, to=100, width=6, textvariable=var, state=state).grid(row=r, column=1, sticky="w")
                ttk.Button(frm, text="Set", width=5, state=state,
                           command=lambda rl=rule, v=var, k=kind: self._send_gamerule(rl, v, k)).grid(row=r, column=2, sticky="w", padx=4)
            r += 1
        ttk.Label(frm, text="Tip: playerssleepingpercentage 0 = one sleeper is enough to skip the night.",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(frm, text="Close", command=dlg.destroy).grid(row=r + 1, column=0, columnspan=3, pady=(10, 0))

    def _send_gamerule(self, rule: str, var, kind: str):
        if not (self.server_manager and self.server_manager.is_running()):
            messagebox.showwarning("Server stopped", "Start the Server to change gamerules.")
            return
        try:
            value = ("true" if var.get() else "false") if kind == "bool" else str(max(0, min(100, int(var.get()))))
        except Exception:
            messagebox.showerror("Gamerules", "Enter a number between 0 and 100.")
            return
        self.server_manager.send_command(f"gamerule {rule} {value}")
        self.console_log(f"> gamerule {rule} {value}")
        self.log(f"gamerule {rule} = {value}", "success")

    def setup_settings_tab(self):
        self.settings_tab.columnconfigure(0, weight=1)
        location_frame = ttk.LabelFrame(self.settings_tab, text="Server Location", padding=10)
        location_frame.grid(row=0, column=0, sticky="ew", pady=5)
        location_frame.columnconfigure(1, weight=1)
        ttk.Label(location_frame, text="Server Folder:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.server_entry = ttk.Entry(location_frame)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.server_entry.bind("<KeyRelease>", self.validate_inputs)
        ttk.Button(location_frame, text="Browse", command=self.browse_server).grid(row=0, column=2, padx=5)
        self.server_status = ttk.Label(location_frame, text="", foreground="gray")
        self.server_status.grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(location_frame, text="One-time setup: the folder that holds (or will hold) your Bedrock server.",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=2, column=1, sticky="w", padx=5)
        backup_frame = ttk.LabelFrame(self.settings_tab, text="Backup Settings", padding=10)
        backup_frame.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Label(backup_frame, text="Maximum backups to keep:").grid(row=0, column=0, sticky="w", pady=5)
        self.max_backups_var = tk.IntVar(value=self.config.get("max_backups", 5))
        ttk.Spinbox(backup_frame, from_=1, to=50, width=10, textvariable=self.max_backups_var).grid(row=0, column=1, sticky="w", padx=10)
        self.auto_cleanup_var = tk.BooleanVar(value=self.config.get("auto_cleanup_backups", True))
        ttk.Checkbutton(backup_frame, text="Automatically cleanup old backups after update", variable=self.auto_cleanup_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.compress_var = tk.BooleanVar(value=self.config.get("compress_backups", False))
        ttk.Checkbutton(backup_frame, text="Compress backups (ZIP format, slower but smaller)", variable=self.compress_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        update_frame = ttk.LabelFrame(self.settings_tab, text="Update Settings", padding=10)
        update_frame.grid(row=2, column=0, sticky="ew", pady=5)
        self.auto_stop_var = tk.BooleanVar(value=self.config.get("auto_stop_server_before_update", True))
        ttk.Checkbutton(update_frame, text="Automatically stop server before update", variable=self.auto_stop_var).grid(row=0, column=0, sticky="w", pady=5)
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start_server_after_update", False))
        ttk.Checkbutton(update_frame, text="Automatically start server after update", variable=self.auto_start_var).grid(row=1, column=0, sticky="w", pady=5)
        self.check_updates_var = tk.BooleanVar(value=self.config.get("check_updates_on_start", True))
        ttk.Checkbutton(update_frame, text="Check for server updates on application start", variable=self.check_updates_var).grid(row=2, column=0, sticky="w", pady=5)
        ui_frame = ttk.LabelFrame(self.settings_tab, text="Interface Settings", padding=10)
        ui_frame.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Label(ui_frame, text="Console font size:").grid(row=0, column=0, sticky="w", pady=5)
        self.font_size_var = tk.IntVar(value=self.config.get("console_font_size", 9))
        ttk.Spinbox(ui_frame, from_=6, to=24, width=10, textvariable=self.font_size_var).grid(row=0, column=1, sticky="w", padx=10)
        self.notifications_var = tk.BooleanVar(value=self.config.get("show_notifications", True))
        ttk.Checkbutton(ui_frame, text="Show notification messages", variable=self.notifications_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.dark_mode_var = tk.BooleanVar(value=self.config.get("dark_mode", False))
        ttk.Checkbutton(ui_frame, text="🌙 Dark mode", variable=self.dark_mode_var, command=self.toggle_dark_mode).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        btn_frame = ttk.Frame(self.settings_tab)
        btn_frame.grid(row=4, column=0, sticky="ew", pady=20)
        ttk.Button(btn_frame, text="💾 Save Settings", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Reset to Defaults", command=self.reset_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Open Config File", command=self.open_config_file).pack(side=tk.RIGHT, padx=5)
        about_frame = ttk.LabelFrame(self.settings_tab, text="About", padding=10)
        about_frame.grid(row=5, column=0, sticky="ew", pady=5)
        ttk.Label(about_frame, text=f"{APP_NAME} v{APP_VERSION}").pack(anchor="w")
        ttk.Label(about_frame, text=APP_AUTHOR, foreground="gray").pack(anchor="w")
        ttk.Label(about_frame, text="A comprehensive tool for managing Minecraft Bedrock Dedicated Servers.", foreground="gray").pack(anchor="w", pady=(5, 0))
    
    def create_tooltip(self, widget, text):
        def show_tooltip(event):
            tooltip = tk.Toplevel()
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            label = ttk.Label(tooltip, text=text, background="#ffffe0", relief="solid", borderwidth=1, padding=3)
            label.pack()
            widget._tooltip = tooltip
            widget.after(2000, lambda: tooltip.destroy() if tooltip.winfo_exists() else None)
        def hide_tooltip(event):
            if hasattr(widget, '_tooltip') and widget._tooltip:
                widget._tooltip.destroy()
        widget.bind("<Enter>", show_tooltip)
        widget.bind("<Leave>", hide_tooltip)
    
    def log(self, message: str, level: str = "info"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {message}"
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, formatted + "\n", level)
        self.log_text.see(tk.END)
        max_lines = self.config.get("console_max_lines", 1000)
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > max_lines:
            self.log_text.delete('1.0', f'{lines - max_lines}.0')
        self.log_text.config(state=tk.DISABLED)
        self.logger.info(message)
    
    def console_log(self, message: str):
        self.console_text.config(state=tk.NORMAL)
        self.console_text.insert(tk.END, message + "\n")
        self.console_text.see(tk.END)
        max_lines = self.config.get("console_max_lines", 1000)
        lines = int(self.console_text.index('end-1c').split('.')[0])
        if lines > max_lines:
            self.console_text.delete('1.0', f'{lines - max_lines}.0')
        self.console_text.config(state=tk.DISABLED)
    
    def set_progress(self, value: float, text: str = None):
        self.progress_var.set(value)
        if text:
            self.progress_label.config(text=text)
            self.status_label.config(text=text)
        self.root.update_idletasks()
    
    def apply_theme(self):
        if self.dark_mode_var.get():
            self.style.theme_use("clam")
            bg, fg = "#2b2b2b", "#ffffff"
            self.root.configure(bg=bg)
            self.style.configure(".", background=bg, foreground=fg)
            self.style.configure("TFrame", background=bg)
            self.style.configure("TLabel", background=bg, foreground=fg)
            self.style.configure("TLabelframe", background=bg, foreground=fg)
            self.style.configure("TLabelframe.Label", background=bg, foreground=fg)
            self.style.configure("TCheckbutton", background=bg, foreground=fg)
            self.style.configure("TNotebook", background=bg)
            self.style.configure("TNotebook.Tab", background="#404040", foreground=fg)
            self.style.map("TNotebook.Tab", background=[("selected", "#505050")])
            if hasattr(self, 'log_text'):
                self.log_text.configure(bg="#1e1e1e", fg="#ffffff")
        else:
            self.style.theme_use("clam" if sys.platform != "win32" else "vista")
            self.root.configure(bg="#f0f0f0")
            if hasattr(self, 'log_text'):
                self.log_text.configure(bg="#ffffff", fg="#000000")
    
    def toggle_dark_mode(self):
        self.config["dark_mode"] = self.dark_mode_var.get()
        save_config(self.config)
        self.apply_theme()
    
    def load_saved_state(self):
        if self.config.get("last_zip_path"):
            self.zip_entry.insert(0, self.config["last_zip_path"])
        if self.config.get("last_server_path"):
            self.server_entry.insert(0, self.config["last_server_path"])
            self.initialize_managers()
        self.validate_inputs()
        self.refresh_sidebar()
    
    def on_tab_changed(self, event=None):
        """Refresh the data behind a tab whenever the user opens it.

        Deliberately does NOT reload Active Server Configuration (it would
        discard unsaved edits) or Backups (sizing every backup is slow).
        """
        if not hasattr(self, 'server_entry'):
            return
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return
        if current is self.worlds_tab:
            self.refresh_worlds()
        elif current is self.server_tab:
            self.update_server_info()
        elif current is self.players_tab:
            self.refresh_players_tab()

    def _build_context(self, server_path: Path) -> dict:
        """Construct a fresh registry entry: its own ServerManager, BackupManager,
        and a console ring buffer (for replay once the sidebar can reselect it)."""
        server_manager = ServerManager(server_path)
        console_buffer = deque(maxlen=self.config.get("console_max_lines", 1000))
        server_manager.add_output_callback(lambda line: self.root.after(0, lambda: self.console_log(line)))
        server_manager.add_output_callback(lambda line: self.root.after(0, lambda: self._scan_console_line(line)))
        server_manager.add_output_callback(console_buffer.append)
        server_manager.add_status_callback(lambda status: self.root.after(0, lambda: self.update_server_status(status)))
        return {
            "server_manager": server_manager,
            "backup_manager": BackupManager(server_path, self.config),
            "console_buffer": console_buffer,
        }

    def _get_or_create_context(self, profile_id: str, server_path: Path) -> Optional[dict]:
        """Return the registry entry for profile_id, creating it if needed.

        If a context already exists for this profile_id but points at a
        different (still-running) path -- i.e. the Server Folder was changed
        out from under a running Server -- returns None so the caller can
        refuse the change instead of silently orphaning the running process.
        """
        ctx = self.contexts.get(profile_id)
        if ctx is not None:
            if ctx["server_manager"].server_path == server_path:
                return ctx
            if ctx["server_manager"].is_running():
                return None
        ctx = self._build_context(server_path)
        self.contexts[profile_id] = ctx
        return ctx

    def initialize_managers(self):
        # Ensures active_profile exists (creating it on the very first Server
        # Folder selection) and that its path is current, before the registry
        # needs a key to file this context under.
        self._sync_flat_settings_into_active_profile()
        server_path = Path(self.server_entry.get())
        profile_id = self.config.get("active_profile")
        if not server_path.exists() or not profile_id:
            return
        ctx = self._get_or_create_context(profile_id, server_path)
        if ctx is None:
            old_path = str(self.contexts[profile_id]["server_manager"].server_path)
            messagebox.showwarning("Server Running",
                "The Server at the previous location is still running.\n\n"
                "Stop it before changing the Server Folder.")
            self.server_entry.delete(0, tk.END)
            self.server_entry.insert(0, old_path)
            return
        self.server_manager = ctx["server_manager"]
        self.backup_manager = ctx["backup_manager"]
        # Force-sync the UI to this context's ACTUAL state -- it may already be
        # running (we're switching back to it), and no status-change callback
        # fires just from being reselected.
        self.update_server_status("running" if self.server_manager.is_running() else "stopped")
        if hasattr(self, 'console_text'):
            self.console_text.config(state=tk.NORMAL)
            self.console_text.delete(1.0, tk.END)
            for line in ctx["console_buffer"]:
                self.console_text.insert(tk.END, line + "\n")
            self.console_text.see(tk.END)
            self.console_text.config(state=tk.DISABLED)
        self.refresh_backups()
        self.refresh_worlds()
        self.refresh_world_combo()
        self.refresh_backup_header()
        self.refresh_players_tab()
        self.update_network_info()
    
    def update_server_status(self, status: str):
        if status == "running":
            self.server_running_label.config(text="⬤ Running", foreground="green")
            self.server_status_label.config(text="⬤ Server: Running", foreground="green")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state=tk.DISABLED)
                self.world_hint_label.config(text="Not available until the running Server is stopped")
        else:
            self.server_running_label.config(text="⬤ Stopped", foreground="red")
            self.server_status_label.config(text="⬤ Server: Stopped", foreground="red")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state="readonly")
                self.world_hint_label.config(text="")
        self.refresh_sidebar()

    def update_network_info(self):
        server_path = self.server_entry.get()
        if server_path:
            props = self.parse_server_properties(Path(server_path) / "server.properties")
            port = props.get("server-port", "19132")
            ip = get_local_ip()
            self.network_label.config(text=f"Network: {ip}:{port}")
    
    def _sync_flat_settings_into_active_profile(self):
        """Mirror the flat 'current profile' keys into server_profiles[active_profile].

        Stage 1 (see docs/V2-MAJORDOMO-PLAN.md): there is exactly one selected
        Server, and the flat keys are its working cache. preserve_items and
        known_players are the same dict object as the profile's copy (aliased
        in hydrate_active_profile_cache), so only the scalar settings need
        copying back here before a save. Creates the profile lazily the first
        time a Server Folder is set (no profile exists yet).
        """
        server_path = self.server_entry.get().strip()
        profile_id = self.config.get("active_profile")
        profiles = self.config.setdefault("server_profiles", {})
        profile = profiles.get(profile_id) if profile_id else None
        if profile is None:
            if not server_path:
                return
            profile_id = uuid.uuid4().hex[:8]
            name = _peek_server_name(Path(server_path)) or Path(server_path).name or "Server"
            profile = {
                "name": name,
                "preserve_items": self.config.get("preserve_items", {}),
                "known_players": self.config.get("known_players", {}),
            }
            profiles[profile_id] = profile
            self.config["active_profile"] = profile_id
        profile["path"] = server_path
        for key in ("max_backups", "compress_backups", "auto_cleanup_backups",
                    "auto_stop_server_before_update", "auto_start_server_after_update"):
            if key in self.config:
                profile[key] = self.config[key]

    def on_close(self):
        running = [(pid, ctx) for pid, ctx in self.contexts.items() if ctx["server_manager"].is_running()]
        if running:
            profiles = self.config.get("server_profiles", {})
            names = [profiles.get(pid, {}).get("name", "Server") for pid, _ in running]
            if not messagebox.askyesno("Servers Running",
                    "The following Servers are still running:\n\n"
                    + "\n".join(f"  • {n}" for n in names)
                    + "\n\nStop them and exit?"):
                return
            for pid, ctx in running:
                self.log(f"Stopping '{profiles.get(pid, {}).get('name', 'Server')}' before exit...", "info")
                ctx["server_manager"].stop()
        self.config["window_geometry"] = self.root.geometry()
        self.config["last_zip_path"] = self.zip_entry.get()
        self.config["last_server_path"] = self.server_entry.get()
        for item, var in self.preserve_vars.items():
            if item in self.config["preserve_items"]:
                self.config["preserve_items"][item]["enabled"] = var.get()
        self._sync_flat_settings_into_active_profile()
        save_config(self.config)
        self.root.destroy()
    
    def browse_zip(self):
        initial = get_downloads_folder()
        if self.zip_entry.get():
            initial = str(Path(self.zip_entry.get()).parent)
        filepath = filedialog.askopenfilename(initialdir=initial, title="Select Bedrock Server ZIP",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")])
        if filepath:
            self.zip_entry.delete(0, tk.END)
            self.zip_entry.insert(0, filepath)
            self.validate_inputs()
    
    def browse_server(self):
        initial = str(Path.home())
        if self.server_entry.get():
            initial = self.server_entry.get()
        folderpath = filedialog.askdirectory(initialdir=initial, title="Select Bedrock Server Folder")
        if folderpath:
            self.server_entry.delete(0, tk.END)
            self.server_entry.insert(0, folderpath)
            self.initialize_managers()
            self.validate_inputs()
    
    def validate_inputs(self, event=None):
        zip_path = self.zip_entry.get()
        server_path = self.server_entry.get()
        valid = True
        if zip_path:
            is_valid, msg = is_valid_bedrock_zip(zip_path)
            if is_valid:
                size = format_size(os.path.getsize(zip_path))
                self.zip_status.config(text=f"✅ {msg} ({size})", foreground="green")
            else:
                self.zip_status.config(text=f"❌ {msg}", foreground="red")
                valid = False
        else:
            self.zip_status.config(text="")
            valid = False
        if server_path:
            p_server_path = Path(server_path)
            is_valid, msg = is_valid_bedrock_server(server_path)
            if is_valid:
                version = detect_server_version(p_server_path)
                self.server_status.config(text=f"✅ {msg} | Version: {version}", foreground="green")
                self.update_server_info()
            elif p_server_path.exists() and p_server_path.is_dir() and not any(p_server_path.iterdir()):
                self.server_status.config(text="❓ Folder is empty (Ready for Install)", foreground="blue")
                valid = False
                is_zip_valid, _ = is_valid_bedrock_zip(zip_path)
                if is_zip_valid and not self.is_updating:
                    if messagebox.askyesno("Empty Folder",
                        f"The folder '{p_server_path.name}' is empty.\n\nWould you like to install the Minecraft server here now?"):
                        self.is_first_install = True
                        self.is_updating = True
                        self.update_button.config(state=tk.DISABLED)
                        self.log(f"Starting fresh installation to {server_path}...", "info")
                        threading.Thread(target=self.perform_update, daemon=True).start()
                        return
            else:
                self.server_status.config(text=f"❌ {msg}", foreground="red")
                valid = False
        else:
            self.server_status.config(text="")
            valid = False
        self.update_button.config(state=tk.NORMAL if valid and not self.is_updating else tk.DISABLED)
        if hasattr(self, 'properties_editor'):
            self.properties_editor.load_properties()
    
    def update_server_info(self):
        server_path_str = self.server_entry.get()
        if not server_path_str or not Path(server_path_str).exists():
            self.info_text.config(text="No Server selected — set the Server Folder in ⚙️ Settings.")
            return
        server_path = Path(server_path_str)
        props = self.parse_server_properties(server_path)
        worlds = get_world_info(server_path)
        installed = detect_server_version(server_path)
        active = props.get("level-name", "Bedrock Level")
        world_versions = {w["name"]: w["version"] for w in worlds}
        world_line = f"Active World: {active}"
        if active not in world_versions:
            world_line += " (not generated yet — created on first start)"
        elif world_versions[active] != "Unknown":
            world_line += f" — last run on {world_versions[active]} (won't load on older versions)"
            iv, wv = parse_version_tuple(installed), parse_version_tuple(world_versions[active])
            if iv and wv and wv > iv:
                world_line += "  ⚠ NEWER than installed Bedrock Server Version!"
        info_lines = [
            f"Server Name: {props.get('server-name', 'Unknown')}",
            f"Bedrock Server Version: {installed}",
            world_line,
            f"Game Mode: {props.get('gamemode', 'Unknown')} | Difficulty: {props.get('difficulty', 'Unknown')}",
            f"Max Players: {props.get('max-players', 'Unknown')} | Port: {props.get('server-port', '19132')}",
            f"Worlds: {len(worlds)} | Total size: {format_size(get_folder_size(server_path / 'worlds'))}",
        ]
        self.info_text.config(text="\n".join(info_lines))
        self.refresh_world_combo()
        self.refresh_backup_header()
        if hasattr(self, 'update_installed_label'):
            self.update_installed_label.config(text=f"Installed Bedrock Server Version: {installed}")
    
    def get_preserve_list(self) -> List[str]:
        return [item for item, var in self.preserve_vars.items() if var.get()]
    
    def dry_run(self):
        if not self.zip_entry.get() or not self.server_entry.get():
            messagebox.showwarning("Warning", "Please select both a ZIP file and server folder.")
            return
        self.log("=" * 50, "info")
        self.log("DRY RUN - No changes will be made", "warning")
        self.log("=" * 50, "info")
        preserve = self.get_preserve_list()
        server_path = Path(self.server_entry.get())
        for item in preserve:
            path = server_path / item
            if path.exists():
                size = format_size(get_folder_size(path) if path.is_dir() else path.stat().st_size)
                self.log(f"  ✅ {item} ({size})", "success")
            else:
                self.log(f"  ⚠️ {item} (not found)", "warning")
        self.log("Dry run complete.", "info")
    
    def start_update(self):
        if self.is_updating:
            return
        server_dir = Path(self.server_entry.get())
        is_first_install = not (server_dir / "server.properties").exists()
        if is_first_install:
            if not messagebox.askyesno("First-Time Install",
                f"No existing server found in:\n{server_dir}\n\nWould you like to perform a fresh installation?"):
                return
            self.is_updating = True
            self.update_button.config(state=tk.DISABLED)
            threading.Thread(target=self.perform_update, daemon=True).start()
            return
        preserve = self.get_preserve_list()
        if "worlds" not in preserve:
            if not messagebox.askyesno("Warning", "You haven't selected 'worlds'! Your world data will be DELETED. Continue?"):
                return
        running = bool(self.server_manager and self.server_manager.is_running())
        if running and not self.config.get("auto_stop_server_before_update", True):
            if not messagebox.askyesno("Server Running",
                    "The Server is running. Stop it nicely and continue with the update?"):
                return
        running_note = ""
        if running:
            running_note = "\nThe running Server will be stopped nicely first."
            if self.config.get("auto_start_server_after_update", False):
                running_note += "\nIt will be started again afterwards."
        if not messagebox.askyesno("Confirm",
                f"Update server?\n{running_note}\nItems to preserve: {len(preserve)}\nBackup will be created first."):
            return
        # The actual stop happens inside perform_update's worker thread,
        # so the UI never freezes while the Server shuts down.
        self.is_updating = True
        self.update_button.config(state=tk.DISABLED)
        threading.Thread(target=self.perform_update, daemon=True).start()
    
    def perform_update(self):
        zip_path = Path(self.zip_entry.get())
        server_path = Path(self.server_entry.get())
        preserve = self.get_preserve_list()
        start_time = time.time()
        is_fresh = getattr(self, 'is_first_install', False)
        try:
            self.log("=" * 50, "info")
            self.log("STARTING FRESH INSTALL" if is_fresh else "STARTING SERVER UPDATE", "info")
            self.log("=" * 50, "info")
            if self.server_manager and self.server_manager.is_running():
                self.set_progress(5, "Stopping Server...")
                self.log("Stopping the running Server nicely before the update...", "info")
                self.server_manager.stop()
                self.log("Server stopped.", "success")
            backup_path = "N/A"
            backed_up = []
            if not is_fresh:
                self.set_progress(10, "Creating backup...")
                self.log("Creating backup...", "info")
                success, backup_path, backed_up = self.backup_manager.create_backup(
                    preserve, compress=self.config.get("compress_backups", False),
                    progress_callback=lambda p: self.set_progress(10 + p * 0.2))
                self.log(f"Backup created: {len(backed_up)} items", "success")
                self.set_progress(35, "Removing old files...")
                self.log("Removing old server files...", "info")
                for item in server_path.iterdir():
                    if item.is_dir(): shutil.rmtree(item)
                    else: item.unlink()
            else:
                self.log("Fresh install detected: Skipping backup and cleanup.", "info")
            self.set_progress(50, "Extracting server files...")
            self.log(f"Extracting: {zip_path.name}", "info")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                members = zf.namelist()
                for i, member in enumerate(members):
                    zf.extract(member, server_path)
                    if i % 100 == 0:
                        self.set_progress(50 + (i / len(members)) * 40)
            self.log(f"Extracted {len(members)} files", "success")
            if not is_fresh:
                self.set_progress(90, "Restoring preserved files...")
                self.log("Restoring preserved files...", "info")
                for i, item in enumerate(backed_up):
                    source = backup_path / item if backup_path.is_dir() else None
                    dest = server_path / item
                    if backup_path.suffix == '.zip':
                        with zipfile.ZipFile(backup_path, 'r') as zf:
                            for name in zf.namelist():
                                if name.startswith(item):
                                    zf.extract(name, server_path)
                    else:
                        if dest.exists():
                            if dest.is_dir(): shutil.rmtree(dest)
                            else: dest.unlink()
                        if source.is_dir(): shutil.copytree(source, dest)
                        else: shutil.copy2(source, dest)
            if not is_fresh and self.config.get("auto_cleanup_backups", True):
                deleted = self.backup_manager.cleanup_old_backups(self.config.get("max_backups", 5))
                if deleted > 0:
                    self.log(f"Cleaned up {deleted} old backup(s)", "info")
            elapsed = time.time() - start_time
            self.set_progress(100, "Complete!")
            self.log("=" * 50, "info")
            self.log(f"PROCESS COMPLETED in {format_duration(elapsed)}", "success")
            self.log("=" * 50, "info")
            msg = "Server installed successfully!" if is_fresh else "Server updated successfully!"
            if is_fresh:
                msg += ("\n\nNext step: create your World in the 🌍 Worlds tab,\n"
                        "then review 📝 Configuration before the first start.")
            self.root.after(0, lambda: messagebox.showinfo("Success", f"{msg}\n\nTime: {format_duration(elapsed)}"))
            self.root.after(0, self.validate_inputs)
            self.root.after(0, self.refresh_backups)
            if self.config.get("auto_start_server_after_update", False):
                self.root.after(1000, self.start_server)
        except Exception as e:
            self.log(f"ERROR: {str(e)}", "error")
            self.set_progress(0, "Failed!")
            self.root.after(0, lambda: messagebox.showerror("Error", f"Process failed:\n{str(e)}"))
        finally:
            self.is_updating = False
            self.is_first_install = False
            self.root.after(0, lambda: self.update_button.config(state=tk.NORMAL))
    
    def _find_port_conflict(self) -> Optional[str]:
        """Name of another already-running profile bound to the same
        server-port, or None. See docs/V2-MAJORDOMO-PLAN.md, 'Multi-Server
        local' -- several Servers can run at once, but not on the same port."""
        current_profile_id = self.config.get("active_profile")
        current_port = self.parse_server_properties(
            Path(self.server_entry.get()) / "server.properties").get("server-port", "19132")
        for pid, ctx in self.contexts.items():
            if pid == current_profile_id or not ctx["server_manager"].is_running():
                continue
            other_port = self.parse_server_properties(
                ctx["server_manager"].server_path / "server.properties").get("server-port", "19132")
            if other_port == current_port:
                return self.config.get("server_profiles", {}).get(pid, {}).get("name", "another Server")
        return None

    def start_server(self):
        if not self.server_manager:
            messagebox.showwarning("Warning", "No server configured.")
            return
        if self.server_manager.is_running():
            return
        conflict = self._find_port_conflict()
        if conflict:
            messagebox.showerror("Port already in use",
                f"'{conflict}' is already running on this same port.\n\n"
                "Stop it first, or change one Server's server-port in 📝 Configuration.")
            return
        self.console_text.config(state=tk.NORMAL)
        self.console_text.delete(1.0, tk.END)
        self.console_text.config(state=tk.DISABLED)
        if self.server_manager.start():
            self.log("Server started", "success")
        else:
            self.log("Failed to start server", "error")
    
    def stop_server(self):
        if not self.server_manager or not self.server_manager.is_running():
            return
        self.log("Stopping server...", "info")
        threading.Thread(target=lambda: self.server_manager.stop(), daemon=True).start()
    
    def restart_server(self):
        if not self.server_manager:
            return
        def do_restart():
            if self.server_manager.is_running():
                self.server_manager.stop()
                time.sleep(2)
            self.root.after(0, self.start_server)
        self.log("Restarting server...", "info")
        threading.Thread(target=do_restart, daemon=True).start()
    
    def send_server_command(self, event=None):
        if not self.server_manager or not self.server_manager.is_running():
            return
        cmd = self.cmd_entry.get().strip()
        if cmd:
            self.console_log(f"> {cmd}")
            self.server_manager.send_command(cmd)
            self.cmd_entry.delete(0, tk.END)
    
    def quick_command(self, cmd: str):
        if self.server_manager and self.server_manager.is_running():
            self.console_log(f"> {cmd}")
            self.server_manager.send_command(cmd)
    
    def copy_server_ip(self):
        server_path = self.server_entry.get()
        if server_path:
            props = self.parse_server_properties(Path(server_path) / "server.properties")
            port = props.get("server-port", "19132")
            ip = get_local_ip()
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{ip}:{port}")
            self.log(f"Copied to clipboard: {ip}:{port}", "info")
    
    def manual_backup(self):
        if not self.backup_manager:
            messagebox.showwarning("Warning", "No server configured.")
            return
        preserve = self.get_preserve_list()
        if not preserve:
            messagebox.showwarning("Warning", "No items selected to preserve.")
            return
        self.log("Creating manual backup...", "info")
        self.set_progress(0, "Backing up...")
        def do_backup():
            try:
                success, path, backed_up = self.backup_manager.create_backup(
                    preserve, compress=self.config.get("compress_backups", False),
                    progress_callback=lambda p: self.root.after(0, lambda: self.set_progress(p)))
                self.root.after(0, lambda: self.log(f"Backup created: {path.name}", "success"))
                self.root.after(0, lambda: self.set_progress(100, "Backup complete"))
                self.root.after(0, self.refresh_backups)
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Backup created:\n{path}"))
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Backup failed: {str(e)}", "error"))
                self.root.after(0, lambda: messagebox.showerror("Error", f"Backup failed:\n{str(e)}"))
        threading.Thread(target=do_backup, daemon=True).start()
    
    def refresh_backup_header(self):
        """Name the Server these backups belong to, so it's clear what gets backed up."""
        if not hasattr(self, 'backup_header_label'):
            return
        server_path = self.server_entry.get()
        if server_path and Path(server_path).exists():
            props = self.parse_server_properties(Path(server_path))
            name = props.get("server-name", Path(server_path).name)
            # .backup_dir (attribute), not get_backup_dir() -- just displaying
            # the path shouldn't have the side effect of creating the folder.
            stored_in = self.backup_manager.backup_dir if self.backup_manager else \
                Path(server_path).parent / "bedrock_backups" / Path(server_path).name
            self.backup_header_label.config(
                text=f"Backups for: {name}  —  stored in {stored_in}")
        else:
            self.backup_header_label.config(text="Backups for: (no Server selected)")

    def refresh_backups(self):
        if not self.backup_manager:
            return
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        for backup in self.backup_manager.list_backups():
            self.backup_tree.insert("", tk.END, values=(backup["name"], backup["date"], backup["size"]),
                                   tags=(str(backup["path"]),))
    
    def restore_selected_backup(self):
        selected = self.backup_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No backup selected.")
            return
        item = self.backup_tree.item(selected[0])
        backup_name = item["values"][0]
        backup_path = self.backup_manager.get_backup_dir() / backup_name
        if not messagebox.askyesno("Confirm Restore", f"Restore from backup:\n{backup_name}\n\nThis will overwrite current files!"):
            return
        if self.server_manager and self.server_manager.is_running():
            if not messagebox.askyesno("Server Running", "Stop server to restore?"):
                return
            self.server_manager.stop()
        self.log(f"Restoring from {backup_name}...", "info")
        def do_restore():
            try:
                success, restored = self.backup_manager.restore_backup(backup_path)
                self.root.after(0, lambda: self.log(f"Restored {len(restored)} items", "success"))
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Restored {len(restored)} items"))
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Restore failed: {str(e)}", "error"))
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
        threading.Thread(target=do_restore, daemon=True).start()
    
    def delete_selected_backup(self):
        selected = self.backup_tree.selection()
        if not selected:
            return
        item = self.backup_tree.item(selected[0])
        backup_name = item["values"][0]
        if not messagebox.askyesno("Confirm Delete", f"Delete backup:\n{backup_name}?"):
            return
        backup_path = self.backup_manager.get_backup_dir() / backup_name
        if self.backup_manager.delete_backup(backup_path):
            self.log(f"Deleted: {backup_name}", "info")
            self.refresh_backups()
    
    def open_selected_backup(self):
        selected = self.backup_tree.selection()
        if selected:
            item = self.backup_tree.item(selected[0])
            backup_path = self.backup_manager.get_backup_dir() / item["values"][0]
            if backup_path.exists():
                open_folder(backup_path if backup_path.is_dir() else backup_path.parent)
    
    def cleanup_backups(self):
        if not self.backup_manager:
            return
        max_backups = self.config.get("max_backups", 5)
        deleted = self.backup_manager.cleanup_old_backups(max_backups)
        self.log(f"Cleaned up {deleted} old backup(s)", "info")
        self.refresh_backups()
        messagebox.showinfo("Cleanup", f"Removed {deleted} old backup(s)")
    
    def open_backup_folder(self):
        if self.backup_manager:
            open_folder(self.backup_manager.get_backup_dir())
        elif self.server_entry.get():
            open_folder(Path(self.server_entry.get()).parent)
    
    def refresh_worlds(self):
        if not self.server_entry.get():
            return
        for item in self.world_tree.get_children():
            self.world_tree.delete(item)
        server_path = Path(self.server_entry.get())
        props = self.parse_server_properties(server_path / "server.properties")
        active = props.get("level-name", "")
        existing = set()
        for world in get_world_info(server_path):
            existing.add(world["name"])
            is_active = world["name"] == active
            self.world_tree.insert("", tk.END,
                                   text="✅ ACTIVE" if is_active else "",
                                   values=(world["name"], world["size"], world["last_modified"], world["version"]),
                                   tags=("active",) if is_active else ())
        # The Active World may be created-but-not-generated: show it as a pending row
        # so a freshly created World is visibly "there" before its first start.
        if active and active not in existing:
            self.world_tree.insert("", 0, text="✅ ACTIVE",
                                   values=(active, "—", "created on next start", "⏳ configure, then Start"),
                                   tags=("pending", "active"))
        self.world_tree.tag_configure("pending", foreground="#FF9800")
        # Bold WITHOUT setting a size/family, so the row keeps the same font as the others.
        base = tkfont.nametofont("TkDefaultFont")
        self._active_bold = tkfont.Font(font=base)
        self._active_bold.configure(weight="bold")
        self.world_tree.tag_configure("active", font=self._active_bold)
    
    def on_world_select(self, event):
        selected = self.world_tree.selection()
        if selected:
            item = self.world_tree.item(selected[0])
            self.world_info_label.config(text=f"Selected: {item['values'][0]}")
    
    def open_worlds_folder(self):
        server_path = self.server_entry.get()
        if server_path:
            worlds = Path(server_path) / "worlds"
            if worlds.exists():
                open_folder(worlds)
    
    def save_settings(self):
        self.config["max_backups"] = self.max_backups_var.get()
        self.config["auto_cleanup_backups"] = self.auto_cleanup_var.get()
        self.config["compress_backups"] = self.compress_var.get()
        self.config["auto_stop_server_before_update"] = self.auto_stop_var.get()
        self.config["auto_start_server_after_update"] = self.auto_start_var.get()
        self.config["check_updates_on_start"] = self.check_updates_var.get()
        self.config["console_font_size"] = self.font_size_var.get()
        self.config["show_notifications"] = self.notifications_var.get()
        self._sync_flat_settings_into_active_profile()
        save_config(self.config)
        self.log("Settings saved", "success")
        messagebox.showinfo("Settings", "Settings saved successfully!")
    
    def reset_settings(self):
        if messagebox.askyesno("Reset", "Reset all settings to defaults?"):
            self.config = copy.deepcopy(DEFAULT_SETTINGS)
            save_config(self.config)
            self.log("Settings reset to defaults", "info")
            messagebox.showinfo("Reset", "Settings reset. Restart the app to apply all changes.")
    
    def open_config_file(self):
        open_folder(get_config_path().parent)
    
    def open_server_folder(self):
        if self.server_entry.get():
            open_folder(Path(self.server_entry.get()))
    
    def open_download_page(self):
        if hasattr(self, 'last_manual_url') and self.last_manual_url:
            self.log(f"Starting manual download for version {self.last_manual_version}...", "info")
            self.download_updates(self.last_manual_url)
        else:
            self.log("No version selected. Opening official download page...", "warning")
            webbrowser.open("https://www.minecraft.net/en-us/download/server/bedrock")
    
    def download_updates(self, url: str):
        if not url:
            messagebox.showwarning("Download", "No download URL available.")
            return
        downloads_path = Path(get_downloads_folder())
        filename = url.split("/")[-1]
        if not filename.endswith(".zip"):
            filename = "bedrock-server-latest.zip"
        target_path = downloads_path / filename
        if not messagebox.askyesno("Download", f"Download latest server to:\n{target_path}?"):
            return
        def perform_download():
            try:
                self.log(f"Starting download from: {url}", "info")
                self.progress_label.config(text="Downloading update...")
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req) as response, open(target_path, 'wb') as out_file:
                    total_size = int(response.info().get('Content-Length', 0))
                    downloaded = 0
                    while True:
                        buffer = response.read(8192)
                        if not buffer: break
                        out_file.write(buffer)
                        downloaded += len(buffer)
                        if total_size > 0:
                            percent = (downloaded / total_size) * 100
                            self.root.after(0, lambda p=percent: self.progress_var.set(p))
                self.root.after(0, lambda: self.on_download_complete(str(target_path)))
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Download failed: {str(e)}", "error"))
                self.root.after(0, lambda: messagebox.showerror("Download Error", f"Failed to download: {e}"))
        threading.Thread(target=perform_download, daemon=True).start()

    def on_download_complete(self, file_path: str):
        self.log(f"Download complete: {file_path}", "success")
        self.zip_entry.delete(0, tk.END)
        self.zip_entry.insert(0, file_path)
        self.validate_inputs()
        self.progress_var.set(100)
        self.progress_label.config(text="Download finished!")
        messagebox.showinfo("Download Complete", f"Latest server downloaded to:\n{file_path}\n\nIt has been automatically selected for update.")
    
    def manual_version_input(self):
        self.log("Opening Minecraft Wiki for manual version check...")
        webbrowser.open("https://minecraft.wiki/w/Bedrock_Dedicated_Server")
        v = simpledialog.askstring("Manual Version Input",
                                  "Please enter the latest version from the Wiki\n(e.g., 1.21.51.01):",
                                  parent=self.root)
        if v:
            version = v.strip().lower().replace('v', '')
            if sys.platform == "win32":
                url = f"https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"
            else:
                url = f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{version}.zip"
            self.last_manual_url = url
            self.last_manual_version = version
            self.log(f"Version {version} selected. Ready to download.", "success")
            messagebox.showinfo("Version Set", f"Version {version} is now active.\nClick 'Download Latest' to start the download.")
    
    def check_for_updates(self):
        self.log("Checking for server updates...", "info")
        messagebox.showinfo("Check Updates",
            "To check for the latest Bedrock server version:\n\n"
            "1. Click 'Download Latest' to open the official page\n"
            "2. Compare the version there to your current version\n"
            "3. Download if a newer version is available")
        self.open_download_page()
    
    def check_for_updates_silent(self):
        self.log("Hint: Check for server updates via Tools menu", "info")
    
    def validate_server_files(self):
        if not self.server_entry.get():
            messagebox.showwarning("Warning", "No server configured.")
            return
        server_path = Path(self.server_entry.get())
        issues = []
        for f in [SERVER_EXECUTABLE, "server.properties"]:
            if not (server_path / f).exists():
                issues.append(f"Missing: {f}")
        if sys.platform != "win32":
            exe = server_path / SERVER_EXECUTABLE
            if exe.exists() and not os.access(exe, os.X_OK):
                issues.append(f"{SERVER_EXECUTABLE} is not executable (will be fixed automatically)")
                make_executable(exe)
                self.log(f"Fixed: Made {SERVER_EXECUTABLE} executable", "success")
        if issues:
            self.log("Server validation found issues", "warning")
            messagebox.showwarning("Validation", "Issues found:\n\n" + "\n".join(issues))
        else:
            self.log("Server validation passed", "success")
            messagebox.showinfo("Validation", "All server files look good!")
    
    def show_shortcuts(self):
        messagebox.showinfo("Keyboard Shortcuts", "Ctrl+O  - Open server folder\nCtrl+S  - Create backup\nCtrl+U  - Start update\nF5      - Refresh/validate\nF1      - Show about\n\nServer Console:\nEnter   - Send command")
    
    def show_about(self):
        messagebox.showinfo("About", f"{APP_NAME}\nVersion {APP_VERSION}\n\n{APP_AUTHOR}\n\nA comprehensive cross-platform tool for managing\nMinecraft Bedrock Dedicated Servers.")


# ============================================================================
# SERVER PROPERTIES EDITOR COMPONENT
# ============================================================================

class ServerPropertiesEditor(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.entries = {}
        self._loaded_snapshot = {}
        self.setup_ui()

    def has_unsaved_changes(self) -> bool:
        """Used by the sidebar to guard against silently discarding edits when
        switching Servers (see docs/V2-MAJORDOMO-PLAN.md, 'GUI: sidebar')."""
        if not self.entries:
            return False
        current = {k: e.get() for k, e in self.entries.items()}
        return current != self._loaded_snapshot

    def setup_ui(self):
        header = ttk.Frame(self)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="Active Server Configuration", font=("TkDefaultFont", 12, "bold")).pack(side=tk.LEFT)
        btn_frame = ttk.Frame(header)
        btn_frame.pack(side=tk.RIGHT)
        ttk.Button(btn_frame, text="🔄 Reload", command=self.load_properties).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="💾 Save Changes", command=self.save_properties, style="Primary.TButton").pack(side=tk.LEFT, padx=2)
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def load_properties(self):
        server_path = self.app.server_entry.get()
        if not server_path:
            return
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.entries = {}
        props = self.app.parse_server_properties(Path(server_path) / "server.properties")
        row_index = 0
        for key, value in props.items():
            if key.lower() == "level-name":
                continue
            ttk.Label(self.scrollable_frame, text=key).grid(row=row_index, column=0, sticky="e", padx=5, pady=2)
            entry = ttk.Entry(self.scrollable_frame, width=40)
            entry.insert(0, value)
            entry.grid(row=row_index, column=1, sticky="w", padx=5, pady=2)
            if key == "server-name":
                ttk.Label(self.scrollable_frame, text="(the name players see in their server list)",
                          font=("TkDefaultFont", 8), foreground="gray").grid(row=row_index, column=2, sticky="w", padx=5)
            self.entries[key] = entry
            row_index += 1
        self._loaded_snapshot = {k: e.get() for k, e in self.entries.items()}

    def save_properties(self):
        server_path = self.app.server_entry.get()
        if not server_path:
            messagebox.showerror("Error", "No server folder selected!")
            return
        new_props = {key: entry.get() for key, entry in self.entries.items()}
        success = self.app.save_server_properties(Path(server_path) / "server.properties", new_props)
        if success:
            self._loaded_snapshot = dict(new_props)
            self.app.log("Server properties saved successfully.", "success")
            messagebox.showinfo("Success", "Properties saved!")
        else:
            messagebox.showerror("Error", "Failed to save properties.")


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    # className sets the window's WM_CLASS so Linux desktops (e.g. GNOME) can match
    # the running window to bedrock-server-manager.desktop and reuse its icon.
    root = tk.Tk(className="bedrock-server-manager")
    app = BedrockUpdaterApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
