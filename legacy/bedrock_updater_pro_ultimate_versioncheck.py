#!/usr/bin/env python3
"""
Bedrock Server Updater Pro Ultimate
A comprehensive cross-platform tool for managing Minecraft Bedrock Dedicated Servers.
"""

import os
import sys
import json
import shutil
import zipfile
import hashlib
import threading
import subprocess
import socket
import re
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
import logging
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

APP_NAME = "Bedrock Server Updater Pro"
APP_VERSION = "2.5.0"
APP_AUTHOR = "Built with Claude AI"
CONFIG_FILENAME = ".bedrock_updater_config.json"

MINECRAFT_DOWNLOAD_PAGE = "https://www.minecraft.net/en-us/download/server/bedrock"
MINECRAFT_WIKI_PAGE = "https://minecraft.wiki/w/Bedrock_Dedicated_Server"
MINECRAFT_CDN_WIN = "https://www.minecraft.net/bedrockdedicatedserver/bin-win/bedrock-server-{version}.zip"
MINECRAFT_CDN_LINUX = "https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{version}.zip"

UPDATE_CHECK_INTERVAL_HOURS = 6
AUTO_BACKUP_INTERVAL_MINUTES = 60

DEFAULT_PRESERVE_ITEMS = {
    "worlds": {"enabled": True, "description": "World save data (critical!)", "critical": True},
    "server.properties": {"enabled": True, "description": "Server configuration"},
    "allowlist.json": {"enabled": True, "description": "Allowed players list"},
    "permissions.json": {"enabled": True, "description": "Player permissions/ops"},
    "valid_known_packs.json": {"enabled": True, "description": "Resource/behavior pack registry"},
    "resource_packs": {"enabled": True, "description": "Custom resource packs"},
    "behavior_packs": {"enabled": True, "description": "Custom behavior packs"},
    "world_templates": {"enabled": True, "description": "World templates"},
    "config": {"enabled": True, "description": "Additional config folder"},
}

DEFAULT_SETTINGS = {
    "last_zip_path": "",
    "last_server_path": "",
    "preserve_items": DEFAULT_PRESERVE_ITEMS,
    "max_backups": 5,
    "compress_backups": False,
    "auto_cleanup_backups": True,
    "auto_stop_server_before_update": True,
    "auto_start_server_after_update": False,
    "check_updates_on_start": True,
    "update_check_interval_hours": UPDATE_CHECK_INTERVAL_HOURS,
    "auto_backup_enabled": False,
    "auto_backup_interval_minutes": AUTO_BACKUP_INTERVAL_MINUTES,
    "show_notifications": True,
    "dark_mode": False,
    "window_geometry": "950x750",
    "console_font_size": 9,
    "console_max_lines": 1000,
}

SERVER_EXECUTABLE = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"
SERVER_SIGNATURE_FILES = ["bedrock_server.exe", "bedrock_server", "server.properties"]

# Server.properties documentation for the editor
SERVER_PROPERTIES_INFO = {
    "server-name": {"type": "string", "description": "Server name shown in server list"},
    "gamemode": {"type": "choice", "choices": ["survival", "creative", "adventure"], "description": "Default game mode"},
    "difficulty": {"type": "choice", "choices": ["peaceful", "easy", "normal", "hard"], "description": "Game difficulty"},
    "allow-cheats": {"type": "bool", "description": "Allow commands for all players"},
    "max-players": {"type": "int", "min": 1, "max": 30, "description": "Maximum players allowed"},
    "online-mode": {"type": "bool", "description": "Require Xbox Live authentication"},
    "allow-list": {"type": "bool", "description": "Only allow players in allowlist.json"},
    "server-port": {"type": "int", "min": 1, "max": 65535, "description": "IPv4 port (default: 19132)"},
    "server-portv6": {"type": "int", "min": 1, "max": 65535, "description": "IPv6 port (default: 19133)"},
    "view-distance": {"type": "int", "min": 5, "max": 32, "description": "Max view distance in chunks"},
    "tick-distance": {"type": "int", "min": 4, "max": 12, "description": "Simulation distance in chunks"},
    "player-idle-timeout": {"type": "int", "min": 0, "max": 1440, "description": "Kick idle players (0=disabled)"},
    "level-name": {"type": "string", "description": "World folder name"},
    "level-seed": {"type": "string", "description": "World generation seed"},
    "default-player-permission-level": {"type": "choice", "choices": ["visitor", "member", "operator"], "description": "Default permission for new players"},
    "texturepack-required": {"type": "bool", "description": "Force clients to use server textures"},
    "content-log-file-enabled": {"type": "bool", "description": "Enable content logging"},
    "compression-threshold": {"type": "int", "min": 0, "max": 65535, "description": "Network compression threshold"},
    "server-authoritative-movement": {"type": "choice", "choices": ["client-auth", "server-auth", "server-auth-with-rewind"], "description": "Movement validation mode"},
    "player-movement-score-threshold": {"type": "int", "min": 0, "max": 1000, "description": "Anti-cheat sensitivity"},
    "player-movement-distance-threshold": {"type": "float", "description": "Movement distance threshold"},
    "player-movement-duration-threshold-in-ms": {"type": "int", "description": "Movement duration threshold"},
    "correct-player-movement": {"type": "bool", "description": "Correct invalid movement"},
    "server-authoritative-block-breaking": {"type": "bool", "description": "Server validates block breaking"},
    "emit-server-telemetry": {"type": "bool", "description": "Send telemetry to Mojang"},
    "disable-custom-skins": {"type": "bool", "description": "Disable custom player skins"},
}

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
    config = json.loads(json.dumps(DEFAULT_SETTINGS))  # Deep copy
    try:
        if config_path.exists():
            with open(config_path, 'r') as f:
                saved = json.load(f)
                for key, value in saved.items():
                    if key in config:
                        if isinstance(config[key], dict) and isinstance(value, dict):
                            config[key].update(value)
                        else:
                            config[key] = value
    except Exception:
        pass
    return config

def save_config(config: dict):
    try:
        with open(get_config_path(), 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

def get_downloads_folder() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as key:
                return winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
        except Exception:
            pass
    return str(Path.home() / "Downloads")

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
    # Check version.txt
    version_file = server_path / "version.txt"
    if version_file.exists():
        try:
            return version_file.read_text().strip()
        except Exception:
            pass
    # Check changelog
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

def parse_server_properties(server_path: Path) -> Dict[str, str]:
    props = {}
    prop_file = server_path / "server.properties"
    if prop_file.exists():
        try:
            for line in prop_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    props[key.strip()] = value.strip()
        except Exception:
            pass
    return props

def save_server_properties(server_path: Path, props: Dict[str, str]) -> bool:
    prop_file = server_path / "server.properties"
    try:
        lines = []
        if prop_file.exists():
            for line in prop_file.read_text().splitlines():
                if line.strip().startswith('#') or '=' not in line:
                    lines.append(line)
                else:
                    key = line.split('=', 0)[0].strip()
                    if key in props:
                        lines.append(f"{key}={props[key]}")
                    else:
                        lines.append(line)
        else:
            for key, value in props.items():
                lines.append(f"{key}={value}")
        
        prop_file.write_text('\n'.join(lines))
        return True
    except Exception:
        return False

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Unknown"

def check_port_open(port: int) -> bool:
    """Check if a port is available for binding."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(('', port))
        s.close()
        return True
    except OSError:
        return False

def get_world_info(server_path: Path) -> List[Dict]:
    worlds = []
    worlds_dir = server_path / "worlds"
    if worlds_dir.exists():
        for world_dir in worlds_dir.iterdir():
            if world_dir.is_dir():
                level_dat = world_dir / "level.dat"
                world_info = {
                    "name": world_dir.name,
                    "path": world_dir,
                    "size": format_size(get_folder_size(world_dir)),
                    "size_bytes": get_folder_size(world_dir),
                    "last_modified": "Unknown"
                }
                if level_dat.exists():
                    try:
                        mtime = datetime.fromtimestamp(level_dat.stat().st_mtime)
                        world_info["last_modified"] = mtime.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass
                worlds.append(world_info)
    return sorted(worlds, key=lambda w: w.get("size_bytes", 0), reverse=True)

def open_folder(path: Path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    else:
        subprocess.run(["xdg-open", str(path)])

def open_url(url: str):
    webbrowser.open(url)

def load_json_file(filepath: Path) -> Optional[List | Dict]:
    try:
        if filepath.exists():
            with open(filepath, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return None

def save_json_file(filepath: Path, data) -> bool:
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        return True
    except Exception:
        return False

# ============================================================================
# VERSION CHECKER
# ============================================================================

class VersionChecker:
    """Checks for new Bedrock server versions via Minecraft Wiki."""
    
    def __init__(self):
        self.latest_version: Optional[str] = None
        self.download_url: Optional[str] = None
        self.last_check: Optional[datetime] = None
        self.is_checking = False
        self.is_downloading = False
    
    def get_cdn_url(self, version: str) -> str:
        template = MINECRAFT_CDN_WIN if sys.platform == "win32" else MINECRAFT_CDN_LINUX
        return template.format(version=version)
    
    def parse_version(self, version_str: str) -> Tuple[int, ...]:
        try:
            parts = re.findall(r'\d+', version_str)
            return tuple(int(p) for p in parts[:4])
        except Exception:
            return (0,)
    
    def compare_versions(self, v1: str, v2: str) -> int:
        """Compare versions. Returns: -1 if v1<v2, 0 if equal, 1 if v1>v2"""
        p1, p2 = self.parse_version(v1), self.parse_version(v2)
        if p1 < p2:
            return -1
        elif p1 > p2:
            return 1
        return 0
    
    def check_version_from_wiki(self) -> Optional[str]:
        """Get latest version from Minecraft Wiki."""
        try:
            req = urllib.request.Request(
                MINECRAFT_WIKI_PAGE,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml',
                }
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                html = response.read().decode('utf-8')
                
                # Primary: <b>Release:</b> <a...>1.21.131.1</a>
                pattern = r'<b>Release:</b>\s*<a[^>]*>(\d+\.\d+\.\d+\.?\d*)</a>'
                match = re.search(pattern, html)
                if match:
                    return match.group(1)
                
                # Fallback: Bedrock_Dedicated_Server_X.X.X.X links
                pattern = r'Bedrock_Dedicated_Server_(\d+\.\d+\.\d+\.?\d*)'
                matches = re.findall(pattern, html)
                if matches:
                    versions = [(self.parse_version(v), v) for v in matches]
                    versions.sort(reverse=True)
                    if len(versions) > 1 and versions[0][0][1] > versions[1][0][1] + 2:
                        return versions[1][1]  # Skip preview
                    return versions[0][1]
                        
        except Exception:
            pass
        return None
    
    def check_for_updates(self, current_version: str = None) -> Dict:
        """Check for updates using wiki scraping."""
        if self.is_checking:
            return {"success": False, "error": "Check already in progress"}
        
        self.is_checking = True
        result = {
            "success": False,
            "latest_version": None,
            "download_url": None,
            "is_newer": False,
            "error": None
        }
        
        try:
            wiki_version = self.check_version_from_wiki()
            if wiki_version:
                result["latest_version"] = wiki_version
                result["download_url"] = self.get_cdn_url(wiki_version)
                result["success"] = True
                
                if current_version and current_version != "Unknown":
                    if self.compare_versions(wiki_version, current_version) > 0:
                        result["is_newer"] = True
                
                self.latest_version = wiki_version
                self.download_url = result["download_url"]
            else:
                result["error"] = "Could not determine latest version from wiki."
            
            self.last_check = datetime.now()
                
        except Exception as e:
            result["error"] = str(e)
        finally:
            self.is_checking = False
        
        return result
    
    def download_server(self, url: str, destination: Path, 
                       progress_callback=None) -> Tuple[bool, str]:
        """Download server ZIP with progress."""
        if self.is_downloading:
            return False, "Download already in progress"
        
        self.is_downloading = True
        
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            })
            
            with urllib.request.urlopen(req, timeout=60) as response:
                total_size = int(response.headers.get('Content-Length', 0))
                downloaded = 0
                
                with open(destination, 'wb') as f:
                    while True:
                        buffer = response.read(8192)
                        if not buffer:
                            break
                        f.write(buffer)
                        downloaded += len(buffer)
                        
                        if progress_callback and total_size > 0:
                            progress_callback(downloaded / total_size * 100, downloaded, total_size)
            
            return True, f"Downloaded to {destination}"
            
        except urllib.error.HTTPError as e:
            return False, f"HTTP Error {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            return False, f"URL Error: {e.reason}"
        except Exception as e:
            return False, str(e)
        finally:
            self.is_downloading = False
    
    def get_download_filename(self, version: str = None) -> str:
        if version:
            return f"bedrock-server-{version}.zip"
        return "bedrock-server.zip"

# ============================================================================
# BACKUP MANAGER
# ============================================================================

class BackupManager:
    def __init__(self, server_path: Path, config: dict):
        self.server_path = server_path
        self.config = config
        self.backup_dir = server_path.parent / "bedrock_backups"
    
    def get_backup_dir(self) -> Path:
        self.backup_dir.mkdir(exist_ok=True)
        return self.backup_dir
    
    def list_backups(self) -> List[Dict]:
        backups = []
        if self.backup_dir.exists():
            for item in sorted(self.backup_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
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
        return backups
    
    def create_backup(self, preserve_items: List[str], compress: bool = False,
                      progress_callback=None, label: str = "") -> Tuple[bool, Path, List[str]]:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = f"backup_{timestamp}" + (f"_{label}" if label else "")
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
            if backup_path.exists():
                if backup_path.is_dir():
                    shutil.rmtree(backup_path)
                else:
                    backup_path.unlink()
            raise e
    
    def restore_backup(self, backup_path: Path, progress_callback=None) -> Tuple[bool, List[str]]:
        restored = []
        
        if backup_path.suffix == '.zip':
            with zipfile.ZipFile(backup_path, 'r') as zf:
                members = zf.namelist()
                for i, member in enumerate(members):
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
        self._start_time: Optional[datetime] = None
    
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
    
    def get_uptime(self) -> str:
        if not self._start_time or not self.is_running():
            return "Not running"
        delta = datetime.now() - self._start_time
        return format_duration(delta.total_seconds())
    
    def start(self) -> bool:
        if self.is_running():
            return False
        
        executable = self.server_path / SERVER_EXECUTABLE
        if not executable.exists():
            self._notify_output(f"ERROR: Server executable not found: {executable}")
            return False
        
        try:
            if sys.platform != "win32":
                os.chmod(executable, 0o755)
            
            kwargs = {
                "cwd": str(self.server_path),
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "bufsize": 1,
            }
            
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            
            self.process = subprocess.Popen([str(executable)], **kwargs)
            
            self._running = True
            self._start_time = datetime.now()
            self._notify_status("running")
            self._notify_output("Server starting...")
            
            threading.Thread(target=self._read_output, daemon=True).start()
            
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
            self._start_time = None
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
        
        try:
            self.process.wait(timeout=timeout)
            self._running = False
            return True
        except subprocess.TimeoutExpired:
            self._notify_output("Timeout - forcing termination...")
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
                self._start_time = None
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
        self.version_checker = VersionChecker()
        self.auto_backup_timer = None
        self.update_check_timer = None
        
        self.setup_logging()
        self.setup_styles()
        self.setup_ui()
        self.apply_theme()
        self.load_saved_state()
        self.setup_keyboard_shortcuts()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # Start timers
        if self.config.get("check_updates_on_start", True):
            self.root.after(3000, self.check_for_updates_background)
        self.schedule_update_check()
        self.schedule_auto_backup()
    
    def setup_logging(self):
        log_file = get_log_dir() / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[logging.FileHandler(log_file)]
        )
        self.logger = logging.getLogger(__name__)
    
    def setup_styles(self):
        self.style = ttk.Style()
        self.style.configure("Success.TLabel", foreground="green")
        self.style.configure("Warning.TLabel", foreground="orange")
        self.style.configure("Error.TLabel", foreground="red")
        self.style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"))
    
    def setup_keyboard_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self.browse_server())
        self.root.bind("<Control-s>", lambda e: self.manual_backup())
        self.root.bind("<Control-u>", lambda e: self.start_update())
        self.root.bind("<Control-d>", lambda e: self.download_latest_server())
        self.root.bind("<Control-r>", lambda e: self.one_click_update())
        self.root.bind("<F5>", lambda e: self.validate_inputs())
        self.root.bind("<F1>", lambda e: self.show_about())
    
    def schedule_update_check(self):
        interval_hours = self.config.get("update_check_interval_hours", UPDATE_CHECK_INTERVAL_HOURS)
        interval_ms = interval_hours * 60 * 60 * 1000
        
        if self.update_check_timer:
            self.root.after_cancel(self.update_check_timer)
        
        self.update_check_timer = self.root.after(interval_ms, self._periodic_update_check)
    
    def _periodic_update_check(self):
        if self.config.get("check_updates_on_start", True):
            self.check_for_updates_background()
        self.schedule_update_check()
    
    def schedule_auto_backup(self):
        if self.auto_backup_timer:
            self.root.after_cancel(self.auto_backup_timer)
        
        if self.config.get("auto_backup_enabled", False):
            interval_min = self.config.get("auto_backup_interval_minutes", AUTO_BACKUP_INTERVAL_MINUTES)
            self.auto_backup_timer = self.root.after(interval_min * 60 * 1000, self._auto_backup)
    
    def _auto_backup(self):
        if self.backup_manager and self.server_manager:
            self.log("Running scheduled auto-backup...", "info")
            preserve = self.get_preserve_list()
            try:
                _, path, backed_up = self.backup_manager.create_backup(preserve, label="auto")
                self.log(f"Auto-backup complete: {path.name}", "success")
                
                if self.config.get("auto_cleanup_backups", True):
                    self.backup_manager.cleanup_old_backups(self.config.get("max_backups", 5))
                
                self.refresh_backups()
            except Exception as e:
                self.log(f"Auto-backup failed: {str(e)}", "error")
        
        self.schedule_auto_backup()
    
    def setup_ui(self):
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.geometry(self.config.get("window_geometry", "950x750"))
        self.root.minsize(850, 650)
        
        self.create_menu()
        
        # Notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Tabs
        self.main_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.main_tab, text="🔄 Update")
        self.setup_main_tab()
        
        self.server_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.server_tab, text="🎮 Server")
        self.setup_server_tab()
        
        self.backup_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.backup_tab, text="💾 Backups")
        self.setup_backup_tab()
        
        self.worlds_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.worlds_tab, text="🌍 Worlds")
        self.setup_worlds_tab()
        
        self.players_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.players_tab, text="👥 Players")
        self.setup_players_tab()
        
        self.properties_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.properties_tab, text="⚙️ Properties")
        self.setup_properties_tab()
        
        self.settings_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.settings_tab, text="🔧 Settings")
        self.setup_settings_tab()
        
        # Status bar
        self.status_bar = ttk.Frame(self.root)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        
        self.status_label = ttk.Label(self.status_bar, text="Ready")
        self.status_label.pack(side=tk.LEFT)
        
        self.uptime_label = ttk.Label(self.status_bar, text="")
        self.uptime_label.pack(side=tk.LEFT, padx=20)
        
        self.server_status_label = ttk.Label(self.status_bar, text="⬤ Server: Not configured", foreground="gray")
        self.server_status_label.pack(side=tk.RIGHT)
        
        # Start uptime updater
        self._update_uptime()
    
    def _update_uptime(self):
        if self.server_manager and self.server_manager.is_running():
            self.uptime_label.config(text=f"Uptime: {self.server_manager.get_uptime()}")
        else:
            self.uptime_label.config(text="")
        self.root.after(1000, self._update_uptime)
    
    def create_menu(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open Server Folder...", command=self.browse_server, accelerator="Ctrl+O")
        file_menu.add_command(label="Open Downloads Folder", command=lambda: open_folder(Path(get_downloads_folder())))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        
        # Server menu
        server_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Server", menu=server_menu)
        server_menu.add_command(label="Start Server", command=self.start_server)
        server_menu.add_command(label="Stop Server", command=self.stop_server)
        server_menu.add_command(label="Restart Server", command=self.restart_server)
        server_menu.add_separator()
        server_menu.add_command(label="Open Server Folder", command=self.open_server_folder)
        
        # Update menu
        update_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Update", menu=update_menu)
        update_menu.add_command(label="⚡ One-Click Update", command=self.one_click_update, accelerator="Ctrl+R")
        update_menu.add_separator()
        update_menu.add_command(label="Check for Updates", command=self.check_for_updates)
        update_menu.add_command(label="Download Latest Server", command=self.download_latest_server, accelerator="Ctrl+D")
        update_menu.add_command(label="Open Download Page", command=self.open_download_page)
        
        # Backup menu
        backup_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Backup", menu=backup_menu)
        backup_menu.add_command(label="Create Backup Now", command=self.manual_backup, accelerator="Ctrl+S")
        backup_menu.add_command(label="Open Backups Folder", command=self.open_backup_folder)
        backup_menu.add_separator()
        backup_menu.add_command(label="Cleanup Old Backups", command=self.cleanup_backups)
        
        # Tools menu
        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Validate Server Files", command=self.validate_server_files)
        tools_menu.add_command(label="Check Ports", command=self.check_ports)
        tools_menu.add_separator()
        tools_menu.add_command(label="Open Log Folder", command=lambda: open_folder(get_log_dir()))
        
        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Keyboard Shortcuts", command=self.show_shortcuts)
        help_menu.add_command(label="Bedrock Server Wiki", command=lambda: open_url(MINECRAFT_WIKI_PAGE))
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self.show_about, accelerator="F1")
    
    # ===== MAIN TAB =====
    def setup_main_tab(self):
        self.main_tab.columnconfigure(0, weight=1)
        self.main_tab.rowconfigure(5, weight=1)
        
        # Header
        header = ttk.Frame(self.main_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        ttk.Label(header, text="Bedrock Server Manager", 
                 font=("TkDefaultFont", 14, "bold")).pack(side=tk.LEFT)
        
        self.dark_mode_var = tk.BooleanVar(value=self.config.get("dark_mode", False))
        ttk.Checkbutton(header, text="🌙 Dark", variable=self.dark_mode_var,
                       command=self.toggle_dark_mode).pack(side=tk.RIGHT)
        
        # File Selection
        file_frame = ttk.LabelFrame(self.main_tab, text="File Selection", padding=10)
        file_frame.grid(row=1, column=0, sticky="ew", pady=5)
        file_frame.columnconfigure(1, weight=1)
        
        ttk.Label(file_frame, text="Server Folder:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.server_entry = ttk.Entry(file_frame)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.server_entry.bind("<KeyRelease>", self.validate_inputs)
        ttk.Button(file_frame, text="Browse", command=self.browse_server).grid(row=0, column=2, padx=5)
        
        self.server_status = ttk.Label(file_frame, text="", foreground="gray")
        self.server_status.grid(row=1, column=1, sticky="w", padx=5)
        
        ttk.Label(file_frame, text="Update ZIP:").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.zip_entry = ttk.Entry(file_frame)
        self.zip_entry.grid(row=2, column=1, sticky="ew", padx=5)
        self.zip_entry.bind("<KeyRelease>", self.validate_inputs)
        ttk.Button(file_frame, text="Browse", command=self.browse_zip).grid(row=2, column=2, padx=5)
        
        self.zip_status = ttk.Label(file_frame, text="", foreground="gray")
        self.zip_status.grid(row=3, column=1, sticky="w", padx=5)
        
        # Update notification banner (hidden by default)
        self.update_banner = ttk.Frame(self.main_tab)
        self.update_banner_visible = False
        
        banner_inner = ttk.Frame(self.update_banner)
        banner_inner.pack(fill=tk.X, padx=10, pady=5)
        
        self.update_banner_label = ttk.Label(banner_inner, text="🔔 Update available!", 
                                             font=("TkDefaultFont", 10, "bold"), foreground="green")
        self.update_banner_label.pack(side=tk.LEFT)
        
        self.update_banner_version = ttk.Label(banner_inner, text="")
        self.update_banner_version.pack(side=tk.LEFT, padx=(10, 0))
        
        ttk.Button(banner_inner, text="⚡ One-Click Update", command=self.one_click_update).pack(side=tk.RIGHT, padx=5)
        ttk.Button(banner_inner, text="⬇️ Download", command=self.download_latest_server).pack(side=tk.RIGHT, padx=5)
        ttk.Button(banner_inner, text="✕", command=self.hide_update_banner, width=3).pack(side=tk.RIGHT)
        
        # Server Info
        self.info_frame = ttk.LabelFrame(self.main_tab, text="Server Information", padding=10)
        self.info_frame.grid(row=3, column=0, sticky="ew", pady=5)
        self.info_text = ttk.Label(self.info_frame, text="Select a server folder to view information.")
        self.info_text.pack(anchor="w")
        
        # Preserve options
        preserve_frame = ttk.LabelFrame(self.main_tab, text="Files to Preserve During Update", padding=10)
        preserve_frame.grid(row=4, column=0, sticky="ew", pady=5)
        
        preserve_inner = ttk.Frame(preserve_frame)
        preserve_inner.pack(fill=tk.X)
        
        col, row = 0, 0
        for item, props in self.config["preserve_items"].items():
            var = tk.BooleanVar(value=props["enabled"])
            self.preserve_vars[item] = var
            text = f"⭐ {item}" if props.get("critical") else item
            cb = ttk.Checkbutton(preserve_inner, text=text, variable=var)
            cb.grid(row=row, column=col, sticky="w", padx=10, pady=2)
            col += 1
            if col >= 4:
                col = 0
                row += 1
        
        # Progress
        progress_frame = ttk.Frame(self.main_tab)
        progress_frame.grid(row=5, column=0, sticky="ew", pady=10)
        progress_frame.columnconfigure(0, weight=1)
        
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky="ew", pady=5)
        
        self.progress_label = ttk.Label(progress_frame, text="Ready")
        self.progress_label.grid(row=1, column=0, sticky="w")
        
        # Buttons
        button_frame = ttk.Frame(self.main_tab)
        button_frame.grid(row=6, column=0, sticky="ew", pady=5)
        
        ttk.Button(button_frame, text="📋 Dry Run", command=self.dry_run).pack(side=tk.LEFT, padx=5)
        
        self.update_button = ttk.Button(button_frame, text="🚀 Update Server", 
                                        command=self.start_update, style="Primary.TButton")
        self.update_button.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="⚡ One-Click Update", command=self.one_click_update).pack(side=tk.LEFT, padx=5)
        
        ttk.Button(button_frame, text="🔍 Check Updates", command=self.check_for_updates).pack(side=tk.RIGHT, padx=5)
        ttk.Button(button_frame, text="⬇️ Download", command=self.download_latest_server).pack(side=tk.RIGHT, padx=5)
        
        # Log
        log_frame = ttk.LabelFrame(self.main_tab, text="Activity Log", padding=5)
        log_frame.grid(row=7, column=0, sticky="nsew", pady=5)
        self.main_tab.rowconfigure(7, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        
        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.grid(row=0, column=1, sticky="ns")
        
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED,
                               font=("Consolas" if sys.platform == "win32" else "Monaco", 
                                    self.config.get("console_font_size", 9)))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.config(command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        
        self.log_text.tag_config("info", foreground="#2196F3")
        self.log_text.tag_config("success", foreground="#4CAF50")
        self.log_text.tag_config("warning", foreground="#FF9800")
        self.log_text.tag_config("error", foreground="#F44336")
        
        self.log("Application started.", "info")
    
    # ===== SERVER TAB =====
    def setup_server_tab(self):
        self.server_tab.columnconfigure(0, weight=1)
        self.server_tab.rowconfigure(1, weight=1)
        
        # Control panel
        control_frame = ttk.LabelFrame(self.server_tab, text="Server Control", padding=10)
        control_frame.grid(row=0, column=0, sticky="ew", pady=5)
        
        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(fill=tk.X)
        
        self.start_btn = ttk.Button(btn_frame, text="▶️ Start", command=self.start_server, width=12)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="⏹️ Stop", command=self.stop_server, width=12)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        
        self.restart_btn = ttk.Button(btn_frame, text="🔄 Restart", command=self.restart_server, width=12)
        self.restart_btn.pack(side=tk.LEFT, padx=5)
        
        self.server_running_label = ttk.Label(btn_frame, text="⬤ Stopped", foreground="red")
        self.server_running_label.pack(side=tk.RIGHT, padx=20)
        
        # Network info
        net_frame = ttk.Frame(control_frame)
        net_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.network_label = ttk.Label(net_frame, text="Network: Not configured")
        self.network_label.pack(side=tk.LEFT)
        
        ttk.Button(net_frame, text="📋 Copy IP", command=self.copy_server_ip).pack(side=tk.RIGHT)
        ttk.Button(net_frame, text="🔍 Check Ports", command=self.check_ports).pack(side=tk.RIGHT, padx=5)
        
        # Console
        console_frame = ttk.LabelFrame(self.server_tab, text="Server Console", padding=5)
        console_frame.grid(row=1, column=0, sticky="nsew", pady=5)
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)
        
        console_scroll = ttk.Scrollbar(console_frame)
        console_scroll.grid(row=0, column=1, sticky="ns")
        
        self.console_text = tk.Text(console_frame, wrap=tk.WORD, state=tk.DISABLED,
                                   font=("Consolas" if sys.platform == "win32" else "Monaco",
                                        self.config.get("console_font_size", 9)),
                                   bg="#1e1e1e", fg="#ffffff", insertbackground="#ffffff")
        self.console_text.grid(row=0, column=0, sticky="nsew")
        console_scroll.config(command=self.console_text.yview)
        self.console_text.config(yscrollcommand=console_scroll.set)
        
        # Command input
        cmd_frame = ttk.Frame(console_frame)
        cmd_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        cmd_frame.columnconfigure(0, weight=1)
        
        self.cmd_entry = ttk.Entry(cmd_frame)
        self.cmd_entry.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.cmd_entry.bind("<Return>", self.send_server_command)
        
        ttk.Button(cmd_frame, text="Send", command=self.send_server_command).grid(row=0, column=1)
        
        # Quick commands
        quick_frame = ttk.Frame(console_frame)
        quick_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        
        ttk.Label(quick_frame, text="Quick:").pack(side=tk.LEFT, padx=(0, 5))
        for cmd in ["list", "save hold", "save resume", "say Hello!", "help"]:
            ttk.Button(quick_frame, text=cmd, width=10,
                      command=lambda c=cmd: self.quick_command(c)).pack(side=tk.LEFT, padx=2)
    
    # ===== BACKUP TAB =====
    def setup_backup_tab(self):
        self.backup_tab.columnconfigure(0, weight=1)
        self.backup_tab.rowconfigure(1, weight=1)
        
        # Controls
        control_frame = ttk.Frame(self.backup_tab)
        control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        ttk.Button(control_frame, text="💾 Create Backup", command=self.manual_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="🗑️ Cleanup Old", command=self.cleanup_backups).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="📂 Open Folder", command=self.open_backup_folder).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="🔄 Refresh", command=self.refresh_backups).pack(side=tk.RIGHT, padx=5)
        
        # Backup list
        list_frame = ttk.LabelFrame(self.backup_tab, text="Available Backups", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        
        columns = ("name", "date", "size")
        self.backup_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        
        self.backup_tree.heading("name", text="Backup Name")
        self.backup_tree.heading("date", text="Date Created")
        self.backup_tree.heading("size", text="Size")
        
        self.backup_tree.column("name", width=350)
        self.backup_tree.column("date", width=150)
        self.backup_tree.column("size", width=100)
        
        backup_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.backup_tree.yview)
        self.backup_tree.configure(yscrollcommand=backup_scroll.set)
        
        self.backup_tree.grid(row=0, column=0, sticky="nsew")
        backup_scroll.grid(row=0, column=1, sticky="ns")
        
        # Actions
        action_frame = ttk.Frame(self.backup_tab)
        action_frame.grid(row=2, column=0, sticky="ew", pady=10)
        
        ttk.Button(action_frame, text="🔄 Restore Selected", command=self.restore_selected_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="🗑️ Delete Selected", command=self.delete_selected_backup).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="📂 Open Selected", command=self.open_selected_backup).pack(side=tk.LEFT, padx=5)
    
    # ===== WORLDS TAB =====
    def setup_worlds_tab(self):
        self.worlds_tab.columnconfigure(0, weight=1)
        self.worlds_tab.rowconfigure(1, weight=1)
        
        control_frame = ttk.Frame(self.worlds_tab)
        control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        
        ttk.Button(control_frame, text="🔄 Refresh", command=self.refresh_worlds).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="📂 Open Worlds Folder", command=self.open_worlds_folder).pack(side=tk.LEFT, padx=5)
        
        list_frame = ttk.LabelFrame(self.worlds_tab, text="Worlds", padding=10)
        list_frame.grid(row=1, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        
        columns = ("name", "size", "last_modified")
        self.world_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=15)
        
        self.world_tree.heading("name", text="World Name")
        self.world_tree.heading("size", text="Size")
        self.world_tree.heading("last_modified", text="Last Modified")
        
        self.world_tree.column("name", width=350)
        self.world_tree.column("size", width=100)
        self.world_tree.column("last_modified", width=150)
        
        world_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.world_tree.yview)
        self.world_tree.configure(yscrollcommand=world_scroll.set)
        
        self.world_tree.grid(row=0, column=0, sticky="nsew")
        world_scroll.grid(row=0, column=1, sticky="ns")
    
    # ===== PLAYERS TAB =====
    def setup_players_tab(self):
        self.players_tab.columnconfigure(0, weight=1)
        self.players_tab.columnconfigure(1, weight=1)
        self.players_tab.rowconfigure(0, weight=1)
        
        # Allowlist
        allow_frame = ttk.LabelFrame(self.players_tab, text="Allowlist (allowlist.json)", padding=10)
        allow_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=5)
        allow_frame.columnconfigure(0, weight=1)
        allow_frame.rowconfigure(0, weight=1)
        
        self.allowlist_text = tk.Text(allow_frame, width=40, height=20,
                                      font=("Consolas" if sys.platform == "win32" else "Monaco", 9))
        self.allowlist_text.grid(row=0, column=0, sticky="nsew")
        
        allow_scroll = ttk.Scrollbar(allow_frame, command=self.allowlist_text.yview)
        allow_scroll.grid(row=0, column=1, sticky="ns")
        self.allowlist_text.config(yscrollcommand=allow_scroll.set)
        
        allow_btn_frame = ttk.Frame(allow_frame)
        allow_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        
        ttk.Button(allow_btn_frame, text="➕ Add Player", command=self.add_to_allowlist).pack(side=tk.LEFT, padx=2)
        ttk.Button(allow_btn_frame, text="💾 Save", command=self.save_allowlist).pack(side=tk.LEFT, padx=2)
        ttk.Button(allow_btn_frame, text="🔄 Reload", command=self.load_allowlist).pack(side=tk.LEFT, padx=2)
        
        # Permissions
        perm_frame = ttk.LabelFrame(self.players_tab, text="Permissions (permissions.json)", padding=10)
        perm_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=5)
        perm_frame.columnconfigure(0, weight=1)
        perm_frame.rowconfigure(0, weight=1)
        
        self.permissions_text = tk.Text(perm_frame, width=40, height=20,
                                        font=("Consolas" if sys.platform == "win32" else "Monaco", 9))
        self.permissions_text.grid(row=0, column=0, sticky="nsew")
        
        perm_scroll = ttk.Scrollbar(perm_frame, command=self.permissions_text.yview)
        perm_scroll.grid(row=0, column=1, sticky="ns")
        self.permissions_text.config(yscrollcommand=perm_scroll.set)
        
        perm_btn_frame = ttk.Frame(perm_frame)
        perm_btn_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        
        ttk.Button(perm_btn_frame, text="➕ Add Operator", command=self.add_operator).pack(side=tk.LEFT, padx=2)
        ttk.Button(perm_btn_frame, text="💾 Save", command=self.save_permissions).pack(side=tk.LEFT, padx=2)
        ttk.Button(perm_btn_frame, text="🔄 Reload", command=self.load_permissions).pack(side=tk.LEFT, padx=2)
    
    # ===== PROPERTIES TAB =====
    def setup_properties_tab(self):
        self.properties_tab.columnconfigure(0, weight=1)
        self.properties_tab.rowconfigure(0, weight=1)
        
        # Main container with scrollbar
        container = ttk.Frame(self.properties_tab)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        
        # Canvas for scrolling
        canvas = tk.Canvas(container)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.properties_frame = ttk.Frame(canvas)
        
        canvas.configure(yscrollcommand=scrollbar.set)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        canvas_frame = canvas.create_window((0, 0), window=self.properties_frame, anchor="nw")
        
        def on_frame_configure(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
        
        def on_canvas_configure(event):
            canvas.itemconfig(canvas_frame, width=event.width)
        
        self.properties_frame.bind("<Configure>", on_frame_configure)
        canvas.bind("<Configure>", on_canvas_configure)
        
        # Property widgets will be created when server is loaded
        self.property_widgets = {}
        
        ttk.Label(self.properties_frame, text="Select a server folder to edit properties.",
                 foreground="gray").pack(pady=20)
        
        # Buttons
        btn_frame = ttk.Frame(self.properties_tab)
        btn_frame.grid(row=1, column=0, sticky="ew", pady=10)
        
        ttk.Button(btn_frame, text="💾 Save Properties", command=self.save_properties_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Reload", command=self.load_properties_editor).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📄 Edit Raw File", command=self.edit_raw_properties).pack(side=tk.RIGHT, padx=5)
    
    # ===== SETTINGS TAB =====
    def setup_settings_tab(self):
        self.settings_tab.columnconfigure(0, weight=1)
        
        # Backup settings
        backup_frame = ttk.LabelFrame(self.settings_tab, text="Backup Settings", padding=10)
        backup_frame.grid(row=0, column=0, sticky="ew", pady=5)
        
        ttk.Label(backup_frame, text="Max backups to keep:").grid(row=0, column=0, sticky="w", pady=5)
        self.max_backups_var = tk.IntVar(value=self.config.get("max_backups", 5))
        ttk.Spinbox(backup_frame, from_=1, to=50, width=10, textvariable=self.max_backups_var).grid(row=0, column=1, sticky="w", padx=10)
        
        self.auto_cleanup_var = tk.BooleanVar(value=self.config.get("auto_cleanup_backups", True))
        ttk.Checkbutton(backup_frame, text="Auto cleanup old backups after update",
                       variable=self.auto_cleanup_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        
        self.compress_var = tk.BooleanVar(value=self.config.get("compress_backups", False))
        ttk.Checkbutton(backup_frame, text="Compress backups (ZIP)",
                       variable=self.compress_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        
        self.auto_backup_var = tk.BooleanVar(value=self.config.get("auto_backup_enabled", False))
        ttk.Checkbutton(backup_frame, text="Enable scheduled auto-backup",
                       variable=self.auto_backup_var).grid(row=3, column=0, sticky="w", pady=5)
        
        ttk.Label(backup_frame, text="Auto-backup interval (minutes):").grid(row=4, column=0, sticky="w", pady=5)
        self.auto_backup_interval_var = tk.IntVar(value=self.config.get("auto_backup_interval_minutes", 60))
        ttk.Spinbox(backup_frame, from_=5, to=1440, width=10, textvariable=self.auto_backup_interval_var).grid(row=4, column=1, sticky="w", padx=10)
        
        # Update settings
        update_frame = ttk.LabelFrame(self.settings_tab, text="Update Settings", padding=10)
        update_frame.grid(row=1, column=0, sticky="ew", pady=5)
        
        self.auto_stop_var = tk.BooleanVar(value=self.config.get("auto_stop_server_before_update", True))
        ttk.Checkbutton(update_frame, text="Auto stop server before update",
                       variable=self.auto_stop_var).grid(row=0, column=0, sticky="w", pady=5)
        
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start_server_after_update", False))
        ttk.Checkbutton(update_frame, text="Auto start server after update",
                       variable=self.auto_start_var).grid(row=1, column=0, sticky="w", pady=5)
        
        self.check_updates_var = tk.BooleanVar(value=self.config.get("check_updates_on_start", True))
        ttk.Checkbutton(update_frame, text="Check for updates on start",
                       variable=self.check_updates_var).grid(row=2, column=0, sticky="w", pady=5)
        
        ttk.Label(update_frame, text="Check interval (hours):").grid(row=3, column=0, sticky="w", pady=5)
        self.update_interval_var = tk.IntVar(value=self.config.get("update_check_interval_hours", 6))
        ttk.Spinbox(update_frame, from_=1, to=168, width=10, textvariable=self.update_interval_var).grid(row=3, column=1, sticky="w", padx=10)
        
        self.last_check_label = ttk.Label(update_frame, text="Last check: Never", foreground="gray")
        self.last_check_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        
        # UI settings
        ui_frame = ttk.LabelFrame(self.settings_tab, text="Interface", padding=10)
        ui_frame.grid(row=2, column=0, sticky="ew", pady=5)
        
        ttk.Label(ui_frame, text="Console font size:").grid(row=0, column=0, sticky="w", pady=5)
        self.font_size_var = tk.IntVar(value=self.config.get("console_font_size", 9))
        ttk.Spinbox(ui_frame, from_=6, to=20, width=10, textvariable=self.font_size_var).grid(row=0, column=1, sticky="w", padx=10)
        
        self.notifications_var = tk.BooleanVar(value=self.config.get("show_notifications", True))
        ttk.Checkbutton(ui_frame, text="Show notification messages",
                       variable=self.notifications_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        
        # Buttons
        btn_frame = ttk.Frame(self.settings_tab)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=20)
        
        ttk.Button(btn_frame, text="💾 Save Settings", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Reset to Defaults", command=self.reset_settings).pack(side=tk.LEFT, padx=5)
        
        # About
        about_frame = ttk.LabelFrame(self.settings_tab, text="About", padding=10)
        about_frame.grid(row=4, column=0, sticky="ew", pady=5)
        
        ttk.Label(about_frame, text=f"{APP_NAME} v{APP_VERSION}").pack(anchor="w")
        ttk.Label(about_frame, text=APP_AUTHOR, foreground="gray").pack(anchor="w")
    
    # ===== HELPER METHODS =====
    
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
        if self.config.get("last_server_path"):
            self.server_entry.insert(0, self.config["last_server_path"])
            self.initialize_managers()
        if self.config.get("last_zip_path"):
            self.zip_entry.insert(0, self.config["last_zip_path"])
        self.validate_inputs()
    
    def initialize_managers(self):
        server_path = Path(self.server_entry.get())
        if server_path.exists():
            self.server_manager = ServerManager(server_path)
            self.server_manager.add_output_callback(lambda line: self.root.after(0, lambda: self.console_log(line)))
            self.server_manager.add_status_callback(lambda status: self.root.after(0, lambda: self.update_server_status(status)))
            self.backup_manager = BackupManager(server_path, self.config)
            
            self.refresh_backups()
            self.refresh_worlds()
            self.update_network_info()
            self.load_allowlist()
            self.load_permissions()
            self.load_properties_editor()
    
    def update_server_status(self, status: str):
        if status == "running":
            self.server_running_label.config(text="⬤ Running", foreground="green")
            self.server_status_label.config(text="⬤ Server: Running", foreground="green")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
        else:
            self.server_running_label.config(text="⬤ Stopped", foreground="red")
            self.server_status_label.config(text="⬤ Server: Stopped", foreground="red")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
    
    def update_network_info(self):
        server_path = self.server_entry.get()
        if server_path:
            props = parse_server_properties(Path(server_path))
            port = props.get("server-port", "19132")
            ip = get_local_ip()
            self.network_label.config(text=f"Network: {ip}:{port}")
    
    def on_close(self):
        if self.server_manager and self.server_manager.is_running():
            if not messagebox.askyesno("Server Running", "Server is still running. Stop and exit?"):
                return
            self.server_manager.stop()
        
        self.config["window_geometry"] = self.root.geometry()
        self.config["last_zip_path"] = self.zip_entry.get()
        self.config["last_server_path"] = self.server_entry.get()
        for item, var in self.preserve_vars.items():
            if item in self.config["preserve_items"]:
                self.config["preserve_items"][item]["enabled"] = var.get()
        save_config(self.config)
        self.root.destroy()
    
    # ===== FILE OPERATIONS =====
    
    def browse_zip(self):
        initial = get_downloads_folder()
        if self.zip_entry.get():
            initial = str(Path(self.zip_entry.get()).parent)
        
        filepath = filedialog.askopenfilename(
            initialdir=initial,
            title="Select Bedrock Server ZIP",
            filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")]
        )
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
            is_valid, msg = is_valid_bedrock_server(server_path)
            if is_valid:
                version = detect_server_version(Path(server_path))
                self.server_status.config(text=f"✅ {msg} | Version: {version}", foreground="green")
                self.update_server_info()
            else:
                self.server_status.config(text=f"❌ {msg}", foreground="red")
                valid = False
        else:
            self.server_status.config(text="")
            valid = False
        
        self.update_button.config(state=tk.NORMAL if valid and not self.is_updating else tk.DISABLED)
    
    def update_server_info(self):
        server_path = Path(self.server_entry.get())
        props = parse_server_properties(server_path)
        worlds = get_world_info(server_path)
        
        info_lines = [
            f"Name: {props.get('server-name', 'Unknown')} | Port: {props.get('server-port', '19132')}",
            f"Mode: {props.get('gamemode', 'Unknown')} | Difficulty: {props.get('difficulty', 'Unknown')} | Max Players: {props.get('max-players', 'Unknown')}",
            f"Worlds: {len(worlds)} | Total: {format_size(get_folder_size(server_path / 'worlds'))}"
        ]
        self.info_text.config(text="\n".join(info_lines))
    
    def get_preserve_list(self) -> List[str]:
        return [item for item, var in self.preserve_vars.items() if var.get()]
    
    # ===== UPDATE OPERATIONS =====
    
    def check_for_updates(self):
        self.log("Checking for updates...", "info")
        self.set_progress(0, "Checking...")
        
        def do_check():
            current = "Unknown"
            if self.server_entry.get():
                current = detect_server_version(Path(self.server_entry.get()))
            
            result = self.version_checker.check_for_updates(current)
            self.root.after(0, lambda: self.handle_update_check_result(result, manual=True))
        
        threading.Thread(target=do_check, daemon=True).start()
    
    def check_for_updates_background(self):
        def do_check():
            current = "Unknown"
            if self.server_entry.get():
                current = detect_server_version(Path(self.server_entry.get()))
            result = self.version_checker.check_for_updates(current)
            self.root.after(0, lambda: self.handle_update_check_result(result, manual=False))
        
        threading.Thread(target=do_check, daemon=True).start()
    
    def handle_update_check_result(self, result: Dict, manual: bool = False):
        self.set_progress(100, "Ready")
        
        if hasattr(self, 'last_check_label'):
            self.last_check_label.config(text=f"Last check: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        
        if result["success"]:
            self.log(f"Latest version: {result['latest_version']}", "info")
            
            if result["is_newer"]:
                self.log(f"🔔 Update available!", "success")
                self.show_update_banner(result["latest_version"])
                
                if manual and self.config.get("show_notifications", True):
                    if messagebox.askyesno("Update Available",
                            f"New version available: {result['latest_version']}\n\nDownload now?"):
                        self.download_latest_server()
            else:
                self.log("Server is up to date.", "success")
                if manual:
                    messagebox.showinfo("Up to Date", f"Your server is up to date!\n\nLatest: {result['latest_version']}")
        else:
            self.log(f"Update check failed: {result.get('error', 'Unknown')}", "warning")
            if manual:
                messagebox.showwarning("Check Failed", f"Could not check for updates:\n\n{result.get('error')}")
    
    def show_update_banner(self, version: str):
        if not self.update_banner_visible:
            self.update_banner.grid(row=2, column=0, sticky="ew", pady=5)
            self.update_banner_visible = True
        self.update_banner_version.config(text=f"Version {version}")
    
    def hide_update_banner(self):
        if self.update_banner_visible:
            self.update_banner.grid_forget()
            self.update_banner_visible = False
    
    def download_latest_server(self):
        if self.version_checker.download_url:
            self._start_download(self.version_checker.download_url, self.version_checker.latest_version)
            return
        
        self.log("Checking for latest version...", "info")
        
        def do_check():
            current = "Unknown"
            if self.server_entry.get():
                current = detect_server_version(Path(self.server_entry.get()))
            
            result = self.version_checker.check_for_updates(current)
            
            def handle():
                if result["success"] and result.get("download_url"):
                    self._start_download(result["download_url"], result["latest_version"])
                else:
                    messagebox.showwarning("Error", "Could not get download URL. Opening download page...")
                    self.open_download_page()
            
            self.root.after(0, handle)
        
        threading.Thread(target=do_check, daemon=True).start()
    
    def _start_download(self, url: str, version: str = None):
        filename = self.version_checker.get_download_filename(version)
        destination = filedialog.asksaveasfilename(
            initialdir=get_downloads_folder(),
            initialfile=filename,
            title="Save Bedrock Server ZIP",
            filetypes=[("ZIP files", "*.zip")],
            defaultextension=".zip"
        )
        
        if not destination:
            return
        
        destination = Path(destination)
        self.log(f"Downloading {filename}...", "info")
        
        # Download dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Downloading")
        dialog.geometry("400x120")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame, text=f"Downloading {filename}...").pack(anchor="w")
        
        progress_var = tk.DoubleVar()
        progress_bar = ttk.Progressbar(frame, variable=progress_var, maximum=100)
        progress_bar.pack(fill=tk.X, pady=10)
        
        status_label = ttk.Label(frame, text="Connecting...")
        status_label.pack(anchor="w")
        
        cancel_flag = {"cancelled": False}
        
        def cancel():
            cancel_flag["cancelled"] = True
            dialog.destroy()
        
        ttk.Button(frame, text="Cancel", command=cancel).pack(side=tk.RIGHT)
        
        def update_progress(progress, downloaded, total):
            if not cancel_flag["cancelled"]:
                progress_var.set(progress)
                status_label.config(text=f"{format_size(downloaded)} / {format_size(total)}")
                dialog.update_idletasks()
        
        def do_download():
            success, msg = self.version_checker.download_server(url, destination, update_progress)
            
            if cancel_flag["cancelled"]:
                if destination.exists():
                    destination.unlink()
                return
            
            def handle():
                dialog.destroy()
                if success:
                    self.log("Download complete!", "success")
                    if messagebox.askyesno("Download Complete", "Use this file to update your server?"):
                        self.zip_entry.delete(0, tk.END)
                        self.zip_entry.insert(0, str(destination))
                        self.validate_inputs()
                        self.hide_update_banner()
                else:
                    self.log(f"Download failed: {msg}", "error")
                    if messagebox.askyesno("Download Failed", f"{msg}\n\nOpen download page?"):
                        self.open_download_page()
            
            self.root.after(0, handle)
        
        threading.Thread(target=do_download, daemon=True).start()
    
    def open_download_page(self):
        open_url(MINECRAFT_DOWNLOAD_PAGE)
        self.log("Opened download page in browser.", "info")
    
    def dry_run(self):
        if not self.server_entry.get():
            messagebox.showwarning("Warning", "Please select a server folder.")
            return
        
        self.log("=" * 40, "info")
        self.log("DRY RUN - No changes will be made", "warning")
        
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
        
        preserve = self.get_preserve_list()
        if "worlds" not in preserve:
            if not messagebox.askyesno("Warning", "You haven't selected 'worlds'! Data will be LOST. Continue?"):
                return
        
        if not messagebox.askyesno("Confirm", f"Update server?\n\nBackup will be created first."):
            return
        
        if self.server_manager and self.server_manager.is_running():
            if self.config.get("auto_stop_server_before_update", True):
                self.log("Stopping server...", "info")
                self.server_manager.stop()
            else:
                if not messagebox.askyesno("Server Running", "Stop server to continue?"):
                    return
                self.server_manager.stop()
        
        self.is_updating = True
        self.update_button.config(state=tk.DISABLED)
        threading.Thread(target=self.perform_update, daemon=True).start()
    
    def perform_update(self):
        zip_path = Path(self.zip_entry.get())
        server_path = Path(self.server_entry.get())
        preserve = self.get_preserve_list()
        start_time = time.time()
        
        try:
            self.log("=" * 40, "info")
            self.log("STARTING UPDATE", "info")
            
            # Backup
            self.set_progress(10, "Creating backup...")
            _, backup_path, backed_up = self.backup_manager.create_backup(
                preserve,
                compress=self.config.get("compress_backups", False),
                progress_callback=lambda p: self.set_progress(10 + p * 0.2)
            )
            self.log(f"Backup: {len(backed_up)} items", "success")
            
            # Clear
            self.set_progress(35, "Removing old files...")
            for item in server_path.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            
            # Extract
            self.set_progress(50, "Extracting...")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                members = zf.namelist()
                for i, member in enumerate(members):
                    zf.extract(member, server_path)
                    if i % 100 == 0:
                        self.set_progress(50 + (i / len(members)) * 30)
            
            # Restore
            self.set_progress(85, "Restoring files...")
            for i, item in enumerate(backed_up):
                source = backup_path / item
                dest = server_path / item
                
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                
                if source.is_dir():
                    shutil.copytree(source, dest)
                else:
                    shutil.copy2(source, dest)
                
                self.set_progress(85 + (i / len(backed_up)) * 10)
            
            # Cleanup
            if self.config.get("auto_cleanup_backups", True):
                deleted = self.backup_manager.cleanup_old_backups(self.config.get("max_backups", 5))
                if deleted:
                    self.log(f"Cleaned up {deleted} old backup(s)", "info")
            
            elapsed = time.time() - start_time
            self.set_progress(100, "Complete!")
            self.log(f"UPDATE COMPLETE ({format_duration(elapsed)})", "success")
            self.log("=" * 40, "info")
            
            self.root.after(0, lambda: messagebox.showinfo("Success", f"Update complete!\nTime: {format_duration(elapsed)}"))
            self.root.after(0, self.refresh_backups)
            self.root.after(0, self.hide_update_banner)
            
            if self.config.get("auto_start_server_after_update", False):
                self.root.after(1000, self.start_server)
            
        except Exception as e:
            self.log(f"ERROR: {str(e)}", "error")
            self.set_progress(0, "Failed!")
            self.root.after(0, lambda: messagebox.showerror("Error", f"Update failed:\n{str(e)}"))
        
        finally:
            self.is_updating = False
            self.root.after(0, lambda: self.update_button.config(state=tk.NORMAL))
    
    def one_click_update(self):
        """Check for updates, download if available, and apply - all in one click."""
        if self.is_updating:
            return
        
        if not self.server_entry.get():
            messagebox.showwarning("Warning", "Please select a server folder first.")
            return
        
        self.log("Starting one-click update...", "info")
        self.set_progress(0, "Checking for updates...")
        
        def do_update():
            try:
                # Check for updates
                current = detect_server_version(Path(self.server_entry.get()))
                result = self.version_checker.check_for_updates(current)
                
                if not result["success"]:
                    self.root.after(0, lambda: messagebox.showerror("Error", f"Update check failed:\n{result.get('error')}"))
                    self.root.after(0, lambda: self.set_progress(0, "Failed"))
                    return
                
                if not result["is_newer"]:
                    self.root.after(0, lambda: messagebox.showinfo("Up to Date", f"Server is already up to date!\n\nCurrent: {current}\nLatest: {result['latest_version']}"))
                    self.root.after(0, lambda: self.set_progress(100, "Ready"))
                    return
                
                # Ask confirmation
                def ask_confirm():
                    return messagebox.askyesno("Update Available",
                        f"New version available!\n\n"
                        f"Current: {current}\n"
                        f"Latest: {result['latest_version']}\n\n"
                        f"Download and install now?")
                
                confirmed = [False]
                def get_confirm():
                    confirmed[0] = ask_confirm()
                
                self.root.after(0, get_confirm)
                time.sleep(0.5)
                while not confirmed[0]:
                    time.sleep(0.1)
                    if not confirmed[0]:
                        break
                
                if not confirmed[0]:
                    self.root.after(0, lambda: self.set_progress(0, "Cancelled"))
                    return
                
                # Download
                self.root.after(0, lambda: self.set_progress(10, "Downloading..."))
                download_path = Path(get_downloads_folder()) / self.version_checker.get_download_filename(result['latest_version'])
                
                def download_progress(p, d, t):
                    self.root.after(0, lambda: self.set_progress(10 + p * 0.3, f"Downloading... {p:.0f}%"))
                
                success, msg = self.version_checker.download_server(result['download_url'], download_path, download_progress)
                
                if not success:
                    self.root.after(0, lambda: messagebox.showerror("Error", f"Download failed:\n{msg}"))
                    self.root.after(0, lambda: self.set_progress(0, "Failed"))
                    return
                
                # Update zip entry and trigger update
                def trigger_update():
                    self.zip_entry.delete(0, tk.END)
                    self.zip_entry.insert(0, str(download_path))
                    self.validate_inputs()
                    self.start_update()
                
                self.root.after(0, trigger_update)
                
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", f"One-click update failed:\n{str(e)}"))
                self.root.after(0, lambda: self.set_progress(0, "Failed"))
        
        threading.Thread(target=do_update, daemon=True).start()
    
    # ===== SERVER CONTROL =====
    
    def start_server(self):
        if not self.server_manager:
            messagebox.showwarning("Warning", "No server configured.")
            return
        
        self.console_text.config(state=tk.NORMAL)
        self.console_text.delete(1.0, tk.END)
        self.console_text.config(state=tk.DISABLED)
        
        if self.server_manager.start():
            self.log("Server started.", "success")
    
    def stop_server(self):
        if self.server_manager and self.server_manager.is_running():
            self.log("Stopping server...", "info")
            threading.Thread(target=self.server_manager.stop, daemon=True).start()
    
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
        if self.server_manager and self.server_manager.is_running():
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
        if self.server_entry.get():
            props = parse_server_properties(Path(self.server_entry.get()))
            port = props.get("server-port", "19132")
            ip = get_local_ip()
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{ip}:{port}")
            self.log(f"Copied: {ip}:{port}", "info")
    
    def check_ports(self):
        if not self.server_entry.get():
            messagebox.showwarning("Warning", "No server configured.")
            return
        
        props = parse_server_properties(Path(self.server_entry.get()))
        port = int(props.get("server-port", 19132))
        portv6 = int(props.get("server-portv6", 19133))
        
        results = []
        for p, name in [(port, "IPv4"), (portv6, "IPv6")]:
            if check_port_open(p):
                results.append(f"✅ Port {p} ({name}): Available")
            else:
                results.append(f"❌ Port {p} ({name}): In use or blocked")
        
        ip = get_local_ip()
        results.append(f"\nLocal IP: {ip}")
        results.append(f"\nPlayers should connect to: {ip}:{port}")
        
        messagebox.showinfo("Port Check", "\n".join(results))
    
    # ===== BACKUP OPERATIONS =====
    
    def manual_backup(self):
        if not self.backup_manager:
            messagebox.showwarning("Warning", "No server configured.")
            return
        
        preserve = self.get_preserve_list()
        self.log("Creating backup...", "info")
        
        def do_backup():
            try:
                _, path, backed_up = self.backup_manager.create_backup(
                    preserve, compress=self.config.get("compress_backups", False), label="manual"
                )
                self.root.after(0, lambda: self.log(f"Backup created: {path.name}", "success"))
                self.root.after(0, self.refresh_backups)
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Backup failed: {str(e)}", "error"))
        
        threading.Thread(target=do_backup, daemon=True).start()
    
    def refresh_backups(self):
        if not self.backup_manager:
            return
        
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        
        for backup in self.backup_manager.list_backups():
            self.backup_tree.insert("", tk.END, values=(backup["name"], backup["date"], backup["size"]))
    
    def restore_selected_backup(self):
        selected = self.backup_tree.selection()
        if not selected:
            return
        
        item = self.backup_tree.item(selected[0])
        name = item["values"][0]
        
        if not messagebox.askyesno("Confirm", f"Restore from {name}?\n\nThis will overwrite current files!"):
            return
        
        if self.server_manager and self.server_manager.is_running():
            self.server_manager.stop()
        
        backup_path = self.backup_manager.get_backup_dir() / name
        
        def do_restore():
            try:
                _, restored = self.backup_manager.restore_backup(backup_path)
                self.root.after(0, lambda: self.log(f"Restored {len(restored)} items", "success"))
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Restored {len(restored)} items"))
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Restore failed: {str(e)}", "error"))
        
        threading.Thread(target=do_restore, daemon=True).start()
    
    def delete_selected_backup(self):
        selected = self.backup_tree.selection()
        if not selected:
            return
        
        item = self.backup_tree.item(selected[0])
        name = item["values"][0]
        
        if messagebox.askyesno("Confirm", f"Delete {name}?"):
            backup_path = self.backup_manager.get_backup_dir() / name
            if self.backup_manager.delete_backup(backup_path):
                self.refresh_backups()
    
    def open_selected_backup(self):
        selected = self.backup_tree.selection()
        if selected:
            item = self.backup_tree.item(selected[0])
            path = self.backup_manager.get_backup_dir() / item["values"][0]
            if path.exists():
                open_folder(path if path.is_dir() else path.parent)
    
    def cleanup_backups(self):
        if self.backup_manager:
            deleted = self.backup_manager.cleanup_old_backups(self.config.get("max_backups", 5))
            self.log(f"Cleaned up {deleted} backup(s)", "info")
            self.refresh_backups()
    
    def open_backup_folder(self):
        if self.backup_manager:
            open_folder(self.backup_manager.get_backup_dir())
    
    # ===== WORLD OPERATIONS =====
    
    def refresh_worlds(self):
        if not self.server_entry.get():
            return
        
        for item in self.world_tree.get_children():
            self.world_tree.delete(item)
        
        for world in get_world_info(Path(self.server_entry.get())):
            self.world_tree.insert("", tk.END, values=(world["name"], world["size"], world["last_modified"]))
    
    def open_worlds_folder(self):
        if self.server_entry.get():
            worlds = Path(self.server_entry.get()) / "worlds"
            if worlds.exists():
                open_folder(worlds)
    
    # ===== PLAYER OPERATIONS =====
    
    def load_allowlist(self):
        if not self.server_entry.get():
            return
        
        self.allowlist_text.delete(1.0, tk.END)
        data = load_json_file(Path(self.server_entry.get()) / "allowlist.json")
        if data:
            self.allowlist_text.insert(1.0, json.dumps(data, indent=2))
    
    def save_allowlist(self):
        if not self.server_entry.get():
            return
        
        try:
            data = json.loads(self.allowlist_text.get(1.0, tk.END))
            if save_json_file(Path(self.server_entry.get()) / "allowlist.json", data):
                self.log("Allowlist saved.", "success")
            else:
                messagebox.showerror("Error", "Failed to save allowlist.")
        except json.JSONDecodeError as e:
            messagebox.showerror("Error", f"Invalid JSON:\n{str(e)}")
    
    def add_to_allowlist(self):
        name = simpledialog.askstring("Add Player", "Enter player name (Xbox Gamertag):")
        if name:
            try:
                data = json.loads(self.allowlist_text.get(1.0, tk.END) or "[]")
                data.append({"name": name, "ignoresPlayerLimit": False})
                self.allowlist_text.delete(1.0, tk.END)
                self.allowlist_text.insert(1.0, json.dumps(data, indent=2))
            except Exception:
                pass
    
    def load_permissions(self):
        if not self.server_entry.get():
            return
        
        self.permissions_text.delete(1.0, tk.END)
        data = load_json_file(Path(self.server_entry.get()) / "permissions.json")
        if data:
            self.permissions_text.insert(1.0, json.dumps(data, indent=2))
    
    def save_permissions(self):
        if not self.server_entry.get():
            return
        
        try:
            data = json.loads(self.permissions_text.get(1.0, tk.END))
            if save_json_file(Path(self.server_entry.get()) / "permissions.json", data):
                self.log("Permissions saved.", "success")
            else:
                messagebox.showerror("Error", "Failed to save permissions.")
        except json.JSONDecodeError as e:
            messagebox.showerror("Error", f"Invalid JSON:\n{str(e)}")
    
    def add_operator(self):
        xuid = simpledialog.askstring("Add Operator", "Enter player XUID:")
        if xuid:
            try:
                data = json.loads(self.permissions_text.get(1.0, tk.END) or "[]")
                data.append({"permission": "operator", "xuid": xuid})
                self.permissions_text.delete(1.0, tk.END)
                self.permissions_text.insert(1.0, json.dumps(data, indent=2))
            except Exception:
                pass
    
    # ===== PROPERTIES OPERATIONS =====
    
    def load_properties_editor(self):
        if not self.server_entry.get():
            return
        
        # Clear existing widgets
        for widget in self.properties_frame.winfo_children():
            widget.destroy()
        self.property_widgets.clear()
        
        props = parse_server_properties(Path(self.server_entry.get()))
        
        if not props:
            ttk.Label(self.properties_frame, text="No properties found.", foreground="gray").pack(pady=20)
            return
        
        row = 0
        for key, value in sorted(props.items()):
            info = SERVER_PROPERTIES_INFO.get(key, {})
            
            frame = ttk.Frame(self.properties_frame)
            frame.pack(fill=tk.X, padx=5, pady=2)
            
            # Label
            label_text = key
            ttk.Label(frame, text=label_text, width=40).pack(side=tk.LEFT)
            
            # Input widget based on type
            prop_type = info.get("type", "string")
            
            if prop_type == "bool":
                var = tk.BooleanVar(value=value.lower() == "true")
                widget = ttk.Checkbutton(frame, variable=var)
                self.property_widgets[key] = ("bool", var)
            elif prop_type == "choice":
                var = tk.StringVar(value=value)
                widget = ttk.Combobox(frame, textvariable=var, values=info.get("choices", []), width=20)
                self.property_widgets[key] = ("choice", var)
            elif prop_type == "int":
                var = tk.StringVar(value=value)
                widget = ttk.Entry(frame, textvariable=var, width=15)
                self.property_widgets[key] = ("int", var)
            else:
                var = tk.StringVar(value=value)
                widget = ttk.Entry(frame, textvariable=var, width=30)
                self.property_widgets[key] = ("string", var)
            
            widget.pack(side=tk.LEFT, padx=5)
            
            # Description tooltip
            if info.get("description"):
                ttk.Label(frame, text=f"({info['description']})", foreground="gray").pack(side=tk.LEFT, padx=5)
            
            row += 1
    
    def save_properties_file(self):
        if not self.server_entry.get():
            return
        
        props = {}
        for key, (prop_type, var) in self.property_widgets.items():
            if prop_type == "bool":
                props[key] = "true" if var.get() else "false"
            else:
                props[key] = var.get()
        
        server_path = Path(self.server_entry.get())
        prop_file = server_path / "server.properties"
        
        try:
            # Read original to preserve comments and order
            lines = []
            if prop_file.exists():
                for line in prop_file.read_text().splitlines():
                    if line.strip().startswith('#') or '=' not in line:
                        lines.append(line)
                    else:
                        key = line.split('=')[0].strip()
                        if key in props:
                            lines.append(f"{key}={props[key]}")
                        else:
                            lines.append(line)
            
            prop_file.write_text('\n'.join(lines))
            self.log("Properties saved.", "success")
            messagebox.showinfo("Success", "Server properties saved.\n\nRestart server for changes to take effect.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{str(e)}")
    
    def edit_raw_properties(self):
        if self.server_entry.get():
            prop_file = Path(self.server_entry.get()) / "server.properties"
            if prop_file.exists():
                if sys.platform == "win32":
                    os.startfile(prop_file)
                elif sys.platform == "darwin":
                    subprocess.run(["open", str(prop_file)])
                else:
                    subprocess.run(["xdg-open", str(prop_file)])
    
    # ===== SETTINGS =====
    
    def save_settings(self):
        self.config["max_backups"] = self.max_backups_var.get()
        self.config["auto_cleanup_backups"] = self.auto_cleanup_var.get()
        self.config["compress_backups"] = self.compress_var.get()
        self.config["auto_backup_enabled"] = self.auto_backup_var.get()
        self.config["auto_backup_interval_minutes"] = self.auto_backup_interval_var.get()
        self.config["auto_stop_server_before_update"] = self.auto_stop_var.get()
        self.config["auto_start_server_after_update"] = self.auto_start_var.get()
        self.config["check_updates_on_start"] = self.check_updates_var.get()
        self.config["update_check_interval_hours"] = self.update_interval_var.get()
        self.config["console_font_size"] = self.font_size_var.get()
        self.config["show_notifications"] = self.notifications_var.get()
        
        save_config(self.config)
        self.schedule_update_check()
        self.schedule_auto_backup()
        
        self.log("Settings saved.", "success")
        messagebox.showinfo("Settings", "Settings saved!")
    
    def reset_settings(self):
        if messagebox.askyesno("Reset", "Reset all settings to defaults?"):
            self.config = json.loads(json.dumps(DEFAULT_SETTINGS))
            save_config(self.config)
            messagebox.showinfo("Reset", "Settings reset. Restart app to apply.")
    
    # ===== TOOLS =====
    
    def validate_server_files(self):
        if not self.server_entry.get():
            messagebox.showwarning("Warning", "No server configured.")
            return
        
        server_path = Path(self.server_entry.get())
        issues = []
        
        if not (server_path / SERVER_EXECUTABLE).exists():
            issues.append(f"Missing: {SERVER_EXECUTABLE}")
        
        if not (server_path / "server.properties").exists():
            issues.append("Missing: server.properties")
        
        if sys.platform != "win32":
            exe = server_path / SERVER_EXECUTABLE
            if exe.exists() and not os.access(exe, os.X_OK):
                issues.append(f"{SERVER_EXECUTABLE} not executable")
        
        if issues:
            messagebox.showwarning("Validation", "Issues found:\n\n" + "\n".join(issues))
        else:
            messagebox.showinfo("Validation", "All server files look good!")
    
    def open_server_folder(self):
        if self.server_entry.get():
            open_folder(Path(self.server_entry.get()))
    
    # ===== DIALOGS =====
    
    def show_shortcuts(self):
        messagebox.showinfo("Shortcuts", """
Ctrl+O  - Open server folder
Ctrl+S  - Create backup
Ctrl+U  - Start update
Ctrl+D  - Download latest
Ctrl+R  - One-click update
F5      - Refresh
F1      - About
""")
    
    def show_about(self):
        messagebox.showinfo("About", f"""
{APP_NAME}
Version {APP_VERSION}

{APP_AUTHOR}

Features:
• One-click update (check + download + install)
• Automatic update checking
• Server control with console
• Backup management
• Player/permission editing
• Server properties editor
• World browser
• Scheduled auto-backups

Cross-platform: Windows, Linux, macOS
""")

# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    root = tk.Tk()
    app = BedrockUpdaterApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()