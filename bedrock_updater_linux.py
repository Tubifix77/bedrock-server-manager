#!/usr/bin/env python3
"""
Bedrock Server Updater Pro Ultimate
A comprehensive cross-platform tool for managing Minecraft Bedrock Dedicated Servers.
Features: Update, backup, restore, run server, auto-cleanup, download updates, and more.
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
import signal
import re
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from pathlib import Path
import logging
from typing import Optional, Dict, List, Tuple
import time

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

APP_NAME = "Bedrock Server Manager"
APP_VERSION = "1.0.3-Linux"
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

# Default settings
DEFAULT_SETTINGS = {
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
    "active_profile": "default",
    "console_font_size": 9,
    "console_max_lines": 1000,
}

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
    config = DEFAULT_SETTINGS.copy()
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

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Tab order: daily use first — Server is the home tab.
        self.server_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.server_tab, text="🎮 Server")
        self.setup_server_tab()

        self.worlds_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.worlds_tab, text="🌍 Worlds")
        self.setup_worlds_tab()

        self.properties_editor = ServerPropertiesEditor(self.notebook, self)
        self.notebook.add(self.properties_editor, text="📝 Active Server Configuration")

        self.backup_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.backup_tab, text="💾 Backups")
        self.setup_backup_tab()

        self.main_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.main_tab, text="🔄 Update")
        self.setup_main_tab()

        self.settings_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.settings_tab, text="⚙️ Settings")
        self.setup_settings_tab()
        
        self.status_bar = ttk.Frame(self.root)
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        self.status_label = ttk.Label(self.status_bar, text="Ready")
        self.status_label.pack(side=tk.LEFT)
        self.server_status_label = ttk.Label(self.status_bar, text="⬤ Server: Not configured", foreground="gray")
        self.server_status_label.pack(side=tk.RIGHT)
    
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
        self.server_running_label = ttk.Label(btn_frame, text="⬤ Stopped", foreground="red")
        self.server_running_label.pack(side=tk.RIGHT, padx=20)
        world_frame = ttk.Frame(control_frame)
        world_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(world_frame, text="Active World:").pack(side=tk.LEFT)
        self.world_combo = ttk.Combobox(world_frame, state="readonly", width=32)
        self.world_combo.pack(side=tk.LEFT, padx=8)
        self.world_combo.bind("<<ComboboxSelected>>", self.on_world_selected)
        ttk.Label(world_frame, text="(switch while stopped — takes effect on next start)",
                  font=("TkDefaultFont", 8), foreground="gray").pack(side=tk.LEFT)
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
        self.world_tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)
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
        if self.server_manager and self.server_manager.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before switching the Active World.")
            return
        if self.set_active_world(name):
            self.refresh_world_combo()

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
                messagebox.showinfo("World Created",
                    f"'{name}' is now the Active World.\n\n"
                    "Bedrock will generate it the first time you start the Server.\n"
                    "Tip: review 📝 Active Server Configuration (seed, gamemode, difficulty)\n"
                    "before the first start — that's when they shape the new World.")

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
    
    def initialize_managers(self):
        server_path = Path(self.server_entry.get())
        if server_path.exists():
            self.server_manager = ServerManager(server_path)
            self.server_manager.add_output_callback(lambda line: self.root.after(0, lambda: self.console_log(line)))
            self.server_manager.add_status_callback(lambda status: self.root.after(0, lambda: self.update_server_status(status)))
            self.backup_manager = BackupManager(server_path, self.config)
            self.refresh_backups()
            self.refresh_worlds()
            self.refresh_world_combo()
            self.refresh_backup_header()
            self.update_network_info()
    
    def update_server_status(self, status: str):
        if status == "running":
            self.server_running_label.config(text="⬤ Running", foreground="green")
            self.server_status_label.config(text="⬤ Server: Running", foreground="green")
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state=tk.DISABLED)
        else:
            self.server_running_label.config(text="⬤ Stopped", foreground="red")
            self.server_status_label.config(text="⬤ Server: Stopped", foreground="red")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state="readonly")
    
    def update_network_info(self):
        server_path = self.server_entry.get()
        if server_path:
            props = self.parse_server_properties(Path(server_path) / "server.properties")
            port = props.get("server-port", "19132")
            ip = get_local_ip()
            self.network_label.config(text=f"Network: {ip}:{port}")
    
    def on_close(self):
        if self.server_manager and self.server_manager.is_running():
            if not messagebox.askyesno("Server Running", "The server is still running. Stop it and exit?"):
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
        if not messagebox.askyesno("Confirm", f"Update server?\n\nItems to preserve: {len(preserve)}\nBackup will be created first."):
            return
        if self.server_manager and self.server_manager.is_running():
            if self.config.get("auto_stop_server_before_update", True):
                self.log("Stopping server before update...", "info")
                self.server_manager.stop()
            else:
                if not messagebox.askyesno("Server Running", "Server is running. Stop it to continue?"):
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
        is_fresh = getattr(self, 'is_first_install', False)
        try:
            self.log("=" * 50, "info")
            self.log("STARTING FRESH INSTALL" if is_fresh else "STARTING SERVER UPDATE", "info")
            self.log("=" * 50, "info")
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
                        "then review 📝 Active Server Configuration before the first start.")
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
    
    def start_server(self):
        if not self.server_manager:
            messagebox.showwarning("Warning", "No server configured.")
            return
        if self.server_manager.is_running():
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
            self.backup_header_label.config(
                text=f"Backups for: {name}  —  stored in {Path(server_path).parent / 'bedrock_backups'}")
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
        for world in get_world_info(Path(self.server_entry.get())):
            self.world_tree.insert("", tk.END, values=(world["name"], world["size"], world["last_modified"], world["version"]))
    
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
        save_config(self.config)
        self.log("Settings saved", "success")
        messagebox.showinfo("Settings", "Settings saved successfully!")
    
    def reset_settings(self):
        if messagebox.askyesno("Reset", "Reset all settings to defaults?"):
            self.config = DEFAULT_SETTINGS.copy()
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
        self.setup_ui()

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
            self.entries[key] = entry
            row_index += 1

    def save_properties(self):
        server_path = self.app.server_entry.get()
        if not server_path:
            messagebox.showerror("Error", "No server folder selected!")
            return
        new_props = {key: entry.get() for key, entry in self.entries.items()}
        success = self.app.save_server_properties(Path(server_path) / "server.properties", new_props)
        if success:
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
