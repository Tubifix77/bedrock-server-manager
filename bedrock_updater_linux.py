#!/usr/bin/env python3
"""
Bedrock Server Updater Pro Ultimate
A comprehensive cross-platform tool for managing Minecraft Bedrock Dedicated Servers.
Features: Update, backup, restore, run server, auto-cleanup, download updates, and more.
"""

# Defer annotation evaluation so type hints like `root: tk.Tk` never touch
# tkinter at import time -- required for the headless `--agent` mode to import
# this module on a box without tkinter/X11 (see the guarded import below).
from __future__ import annotations

import os
import sys
import json
import copy
import uuid
import shutil
import zipfile
import base64
import hashlib
import hmac
import secrets
import queue
import argparse
import threading
import subprocess
import socket
import signal
import re
import webbrowser
import urllib.request
import urllib.error
from datetime import datetime, timedelta
# tkinter is optional: the GUI needs it, but the headless `--agent` host must
# import and run on machines without tkinter/X11. TK_AVAILABLE gates the GUI.
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from tkinter import font as tkfont
    TK_AVAILABLE = True
except Exception:
    tk = ttk = filedialog = messagebox = simpledialog = tkfont = None
    TK_AVAILABLE = False
from pathlib import Path
import logging
from typing import Optional, Dict, List, Tuple
from collections import deque
import time

# Logger for code outside the GUI class (config I/O, ServerManager, the remote
# layer). It writes wherever the GUI's setup_logging() or the agent's
# run_agent() pointed the logging module -- the per-session log file.
_LOG = logging.getLogger("bedrock_server_manager")

# ============================================================================
# CONFIGURATION & CONSTANTS
# ============================================================================

APP_NAME = "Bedrock Server Manager"
APP_VERSION = "4.0"
# Release name, shown next to the version everywhere a user sees it (window
# title, About, Machine page). The wire protocol / app_version fields keep the
# plain APP_VERSION.
APP_RELEASE_NAME = "Crafty Interface"
APP_VERSION_LABEL = f'v{APP_VERSION} "{APP_RELEASE_NAME}"'
APP_AUTHOR = "Tue Wincentz Boas - Built with Claude AI & Gemini 3"
CONFIG_FILENAME = ".bedrock_updater_config.json"
MINECRAFT_DOWNLOAD_PAGE = "https://www.minecraft.net/en-us/download/server/bedrock"

# Single-instance guard. The GUI binds this loopback-only port on startup; a
# second launch (e.g. the desktop shortcut AND the taskbar icon) fails to bind,
# pings the running instance to raise its window, and exits -- instead of
# spinning up a rival GUI that would fight over the one config file and the one
# tracked engine (which is exactly what stranded a real user's session). Bound
# to 127.0.0.1 only, so no Windows firewall prompt; distinct from the
# remote-admin port and any BDS server port; in the private/dynamic range so it
# won't clash with a well-known service. A crashed instance frees it
# automatically (no stale-lock problem, unlike a PID file).
SINGLE_INSTANCE_PORT = 49732
_SI_HELLO = b"BSM-FOCUS\n"
_SI_ACK = b"BSM-OK\n"

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
    "show_notifications": True,
    "dark_mode": False,
    # Settings ▸ Interface: "classic" | "coat" (New coat of paint) | "layout" (New layout).
    "ui_look": "classic",
    "window_geometry": "1200x700",
    "sidebar_collapsed": True,
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
# Who's online right now, from the same console lines. (Network-level checks
# can't count NetherNet players -- they don't show up as separate connections.)
PLAYER_JOIN_LEAVE_RE = re.compile(r"Player\s+(connected|disconnected)\s*:\s*(.+?)\s*,\s*xuid:", re.IGNORECASE)
# Game-mode and Teleport buttons: how long to wait for the Server's console
# answer, and how long that answer stays next to the button.
CONSOLE_REPLY_TIMEOUT_MS = 4000
CONSOLE_REPLY_SHOW_MS = 20000

SERVER_SIGNATURE_FILES = ["bedrock_server.exe", "bedrock_server", "server.properties"]
SERVER_EXECUTABLE = "bedrock_server.exe" if sys.platform == "win32" else "bedrock_server"

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

# Set by `--agent --config PATH` so both load_config() and save_config() target
# the same file the agent was launched with, instead of the per-user default.
_CONFIG_PATH_OVERRIDE = None


def get_config_path() -> Path:
    if _CONFIG_PATH_OVERRIDE:
        return Path(_CONFIG_PATH_OVERRIDE)
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

# Problems load_config() hit, for the GUI to report once logging and the window
# exist (load_config runs before either). The agent logs them as they happen.
_CONFIG_LOAD_PROBLEMS: List[str] = []


def load_config(config_path=None) -> dict:
    config_path = Path(config_path) if config_path else get_config_path()
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
    except Exception as e:
        # An unreadable config used to be silently replaced by defaults and then
        # overwritten on the next save -- losing every Server profile, paired
        # Machine and known player. Keep the damaged file for recovery instead.
        raw_saved = None
        config = copy.deepcopy(DEFAULT_SETTINGS)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        kept = config_path.with_name(f"{config_path.name}.corrupt-{stamp}")
        n = 1
        while kept.exists():  # never overwrite an earlier kept copy
            kept = config_path.with_name(f"{config_path.name}.corrupt-{stamp}-{n}")
            n += 1
        try:
            os.replace(config_path, kept)
            where = f"The damaged file was kept as {kept}"
        except OSError:
            where = "The damaged file could not be moved aside"
        msg = f"Settings file {config_path} could not be read ({e}); starting with default settings. {where}."
        _CONFIG_LOAD_PROBLEMS.append(msg)
        _LOG.error(msg)
    was_v1 = raw_saved is not None and raw_saved.get("config_version", 1) < 2
    if was_v1:
        config = migrate_config_to_v2(config)
        try:
            bak_path = config_path.parent / (config_path.name + ".v1.bak")
            if not bak_path.exists():
                with open(bak_path, 'w') as f:
                    json.dump(raw_saved, f, indent=2)
        except Exception:
            _LOG.exception("Could not write the pre-2.0 config backup")
    return hydrate_active_profile_cache(config)

def save_config(config: dict):
    # Write to a temp file and swap it in, so a crash or power cut mid-save
    # can't leave a half-written settings file behind.
    path = get_config_path()
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, 'w') as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        _LOG.exception("Could not save settings to %s", path)

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

# --- Bedrock Server Version: latest release, download, installed version ---
# Mojang's download-links endpoint (undocumented but widely relied on -- Docker
# images, the Arch package) lists the current release ZIP per platform plus
# previews. A browser-style User-Agent is needed: the CDN rejects script ones.
BEDROCK_LINKS_API = "https://net-secondary.web.minecraft-services.net/api/v1.0/download/links"
HTTP_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# Written into a Server folder by the app after it installs or updates it, and
# refreshed from the engine's own "Version: x" startup line. Mojang's files no
# longer carry the version, which is why the app used to show "Unknown".
VERSION_MARKER = ".bsm_bedrock_version"
_ZIP_VERSION_RE = re.compile(r"bedrock-server-(\d+(?:\.\d+)+)\.zip$", re.IGNORECASE)
_CONSOLE_VERSION_RE = re.compile(r"\]\s*Version:\s*(\d+(?:\.\d+)+)\s*$")


def fetch_latest_bedrock_release(timeout: float = 15) -> Dict[str, str]:
    """The current *release* (never a preview) Bedrock Server Version for this
    platform, as {"version": "1.26.52.3", "url": ".../bedrock-server-1.26.52.3.zip"}.
    Raises on any network or format problem -- callers fall back to manual entry."""
    kind = "serverBedrockWindows" if sys.platform == "win32" else "serverBedrockLinux"
    req = urllib.request.Request(BEDROCK_LINKS_API, headers={"User-Agent": HTTP_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.load(resp)
    for link in data["result"]["links"]:
        if link.get("downloadType") == kind:
            url = link["downloadUrl"]
            m = _ZIP_VERSION_RE.search(url)
            if not m:
                raise ValueError(f"Unexpected download URL: {url}")
            return {"version": m.group(1), "url": url}
    raise ValueError(f"No {kind} entry in Mojang's download list")


def version_from_zip_name(name: str) -> Optional[str]:
    m = _ZIP_VERSION_RE.search(str(name))
    return m.group(1) if m else None


def download_bedrock_zip(url: str, dest_dir: Path, progress_callback=None) -> Path:
    """Download a Bedrock server ZIP into dest_dir and return its path. An
    already-present valid copy of the same file is reused. Downloads go to a
    .part file that is renamed only when complete, so a broken download never
    looks like a valid ZIP."""
    dest_dir = Path(dest_dir)
    target = dest_dir / url.rsplit("/", 1)[-1]
    if target.exists() and is_valid_bedrock_zip(str(target))[0]:
        if progress_callback:
            progress_callback(100.0)
        return target
    part = target.with_name(target.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
        total = int(resp.info().get("Content-Length", 0) or 0)
        done = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress_callback and total:
                progress_callback(done / total * 100)
    ok, msg = is_valid_bedrock_zip(str(part))
    if not ok:
        part.unlink(missing_ok=True)
        raise ValueError(f"Downloaded file is not a Bedrock server ZIP ({msg})")
    os.replace(part, target)
    return target


def record_installed_version(server_path: Path, version: str):
    try:
        (Path(server_path) / VERSION_MARKER).write_text(version.strip() + "\n")
    except OSError:
        _LOG.exception("Could not record the installed Bedrock Server Version in %s", server_path)


# --- Creating a new Server (3.0 "+", docs/V3-SERVERS-PLAN.md §2) -------------
NEW_SERVER_FIRST_PORT = 19134  # 19132/19133 are Bedrock's defaults, kept for Server 1


def _port_in_use(port: int) -> bool:
    """Something on this machine already accepts on TCP `port` (a NetherNet
    Server listens on TCP server-port). A connect test, not a bind, so Windows
    Firewall never prompts for the app itself."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def find_free_server_port(used_ports: set, start: int = NEW_SERVER_FIRST_PORT) -> int:
    """First port p (stepping by 2, so p+1 stays free for server-portv6) that no
    configured Server uses and nothing on this machine is listening on."""
    p = start
    while p < 65000:
        if p not in used_ports and p + 1 not in used_ports and not _port_in_use(p):
            return p
        p += 2
    raise RuntimeError("No free port found for a new Server")


def create_server_folder(zip_path, target: Path, *, server_name: str, gamemode: str, port: int,
                         lan_visible: bool, world_name: Optional[str] = None,
                         copy_world_from: Optional[Path] = None, version: Optional[str] = None,
                         seed: str = ""):
    """Install a fresh, completely separate Server into `target` (which must not
    exist yet) from a Bedrock ZIP, then set it up: name, gamemode, World (new, or
    a copy of an existing one; `seed` shapes a new one), its own port, LAN
    visibility, allowlist off.
    No tkinter; raises on any problem (the caller deletes the half-made folder)."""
    target = Path(target)
    os.makedirs(_long_path(target))  # fails if it already exists -- never reuse a folder
    with zipfile.ZipFile(_long_path(zip_path)) as zf:
        zf.extractall(_long_path(target))
    make_executable(target / SERVER_EXECUTABLE)
    level = world_name or server_name
    if copy_world_from:
        # The copy takes the new Server's name, so the strip doesn't show two
        # Servers with the same World name.
        shutil.copytree(_long_path(Path(copy_world_from)), _long_path(target / "worlds" / level))
    save_server_properties(target / "server.properties", {
        "server-name": server_name,
        "gamemode": gamemode,
        "level-name": level,
        "server-port": str(port),
        "server-portv6": str(port + 1),
        # Only one Server per machine can be LAN-discoverable (UDP 7551 goes to
        # whichever starts first), so it's made deterministic: see §7 of the plan.
        "enable-lan-visibility": "true" if lan_visible else "false",
        # Stock 1.26.5x ships allow-list=true with an empty list: nobody could join.
        "allow-list": "false",
        # Only used when Bedrock generates the World on the first start; always
        # written so a copied World never inherits a stale stock value.
        "level-seed": "" if copy_world_from else seed.strip(),
    })
    if version:
        record_installed_version(target, version)


def _rmtree_report(path: Path) -> List[str]:
    """Delete a folder tree; read-only files (common on Windows) are made
    writable and retried. Returns what couldn't be removed, instead of stopping
    halfway with an exception."""
    failed: List[str] = []

    def retry(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            failed.append(str(p).replace("\\\\?\\", ""))
    if Path(path).exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(_long_path(path), onexc=retry)
        else:
            shutil.rmtree(_long_path(path), onerror=retry)
    return failed


def server_backup_dir(server_path: Path) -> Path:
    """This Server's own backups (see BackupManager) -- not the older shared
    <parent>/bedrock_backups/backup_* entries kept for rolling back to 1.0.4."""
    server_path = Path(server_path)
    return server_path.parent / "bedrock_backups" / server_path.name


def delete_server_files(server_path: Path) -> List[str]:
    """Permanently delete a Server's folder and its own backups (3.0 "−").
    Refuses anything that isn't a Bedrock server folder, so a profile pointing at
    the wrong place can never wipe it. Returns paths that couldn't be removed."""
    server_path = Path(server_path).resolve()
    # Stricter than is_valid_bedrock_server (which accepts any one signature
    # file): deleting needs an engine binary AND server.properties, and never a
    # drive root or a home folder.
    has_engine = (server_path / "bedrock_server").is_file() or (server_path / "bedrock_server.exe").is_file()
    risky = server_path == Path(server_path.anchor) or server_path == Path.home().resolve()
    if risky or not has_engine or not (server_path / "server.properties").is_file():
        raise ValueError(f"{server_path} doesn't look like a Bedrock server folder; nothing was deleted")
    failed = _rmtree_report(server_path)
    failed += _rmtree_report(server_backup_dir(server_path))
    return failed


class UpdateError(Exception):
    """An update that failed. files_changed tells whether the Server's folder
    had already been wiped (False = it's still intact and can be started)."""
    def __init__(self, message: str, files_changed: bool):
        super().__init__(message)
        self.files_changed = files_changed


def swap_server_files(server_path: Path, zip_path: Path, *, backup_manager, preserve: List[str],
                      compress: bool = False, version: Optional[str] = None, fresh: bool = False,
                      keep_backups: Optional[int] = None, progress=None, log=None) -> dict:
    """The destructive heart of Update, for one stopped Server: back up the
    preserved items -> wipe the folder -> extract the ZIP -> restore the
    preserved items -> merge in new server.properties keys -> record the
    version -> trim old backups. A fresh install only extracts. Raises
    UpdateError. progress(pct, text) and log(msg, level) may be called from
    whatever thread this runs on."""
    progress = progress or (lambda pct, text=None: None)
    log = log or (lambda msg, level="info": None)
    server_path, zip_path = Path(server_path), Path(zip_path)
    backup_path, backed_up, wiped = None, [], False
    try:
        if not fresh:
            progress(10, "Creating backup...")
            log("Creating backup...", "info")
            _, backup_path, backed_up = backup_manager.create_backup(
                preserve, compress=compress, progress_callback=lambda p: progress(10 + p * 0.2))
            log(f"Backup created: {len(backed_up)} items", "success")
            progress(35, "Removing old files...")
            log("Removing old server files...", "info")
            wiped = True
            # _long_path everywhere below: Bedrock's resource packs nest deep
            # enough to pass Windows' 260-character path limit under a long
            # install folder.
            for item in server_path.iterdir():
                if item.is_dir(): shutil.rmtree(_long_path(item))
                else: os.remove(_long_path(item))
        else:
            log("Fresh install: skipping backup and cleanup.", "info")
        progress(50, "Extracting server files...")
        log(f"Extracting: {zip_path.name}", "info")
        wiped = True
        with zipfile.ZipFile(_long_path(zip_path), 'r') as zf:
            members = zf.namelist()
            for i, member in enumerate(members):
                zf.extract(member, _long_path(server_path))
                if i % 100 == 0:
                    progress(50 + (i / len(members)) * 40)
        log(f"Extracted {len(members)} files", "success")
        # Keep the new version's stock server.properties before the restore
        # below overwrites it with the user's old one, so any settings this
        # Bedrock Server Version introduced can be merged back in afterwards.
        fresh_props_text = None
        if not fresh and "server.properties" in backed_up:
            fresh_props = server_path / "server.properties"
            if fresh_props.exists():
                fresh_props_text = fresh_props.read_text()
        if not fresh:
            progress(90, "Restoring preserved files...")
            log("Restoring preserved files...", "info")
            for item in backed_up:
                dest = server_path / item
                if backup_path.suffix == '.zip':
                    with zipfile.ZipFile(_long_path(backup_path), 'r') as zf:
                        for name in zf.namelist():
                            if name.startswith(item):
                                zf.extract(name, _long_path(server_path))
                else:
                    source = backup_path / item
                    if dest.exists():
                        if dest.is_dir(): shutil.rmtree(_long_path(dest))
                        else: os.remove(_long_path(dest))
                    if source.is_dir(): shutil.copytree(_long_path(source), _long_path(dest))
                    else: shutil.copy2(_long_path(source), _long_path(dest))
        if version:
            record_installed_version(server_path, version)
        if fresh_props_text:
            for key, value in merge_new_server_properties(fresh_props_text, server_path / "server.properties"):
                log(f"New setting in this Bedrock Server Version, added to server.properties: "
                    f"{key}={value}", "info")
        if not fresh and keep_backups is not None:
            deleted = backup_manager.cleanup_old_backups(keep_backups)
            if deleted > 0:
                log(f"Cleaned up {deleted} old backup(s)", "info")
    except Exception as e:
        where = f" (its backup: {backup_path})" if wiped and backup_path else ""
        raise UpdateError(f"{e}{where}", files_changed=wiped) from e
    return {"backup": backup_path, "backed_up": backed_up}


def detect_server_version(server_path: Path) -> str:
    # What the app itself recorded (install/update, or the engine's startup line).
    marker = Path(server_path) / VERSION_MARKER
    if marker.exists():
        try:
            v = marker.read_text().strip()
            if v:
                return v
        except OSError:
            pass
    # Older fallbacks (Mojang no longer ships a version number in these files).
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


def parse_server_properties(filepath: Path) -> Dict[str, str]:
    """Read server.properties into a key->value dict. Accepts a file or its
    parent dir. Module-level (no app/widgets) so ServerService and the headless
    --agent can use it too; BedrockUpdaterApp.parse_server_properties delegates
    here."""
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
    except Exception:
        pass
    return props


def save_server_properties(filepath: Path, props: Dict[str, str]) -> bool:
    """Write props back into server.properties, preserving comment lines and
    ordering for keys that already exist, appending any new keys."""
    if not filepath.exists():
        try:
            with open(filepath, 'w') as f:
                for k, v in props.items():
                    f.write(f"{k}={v}\n")
            return True
        except Exception:
            return False
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
    except Exception:
        return False


def _active_world_of(profile: dict) -> str:
    """A Server's Active World (level-name), or "" if its folder can't be read."""
    try:
        return parse_server_properties(Path(profile.get("path", "")) / "server.properties").get("level-name", "")
    except Exception:
        return ""


def merge_new_server_properties(fresh_text: str, target: Path) -> List[Tuple[str, str]]:
    """Bring settings introduced by a new Bedrock Server Version into a
    server.properties that an update just restored from backup.

    The update preserves the user's server.properties wholesale, which would
    silently drop any key Mojang adds -- e.g. `transport` in 1.26.5x, without
    which BDS falls back to RakNet and current clients can't connect. For each
    key in the new stock file (`fresh_text`) that `target` lacks, append Mojang's
    own block (the key line plus the comment lines under it). The user's existing
    values always win and obsolete keys are left alone. Returns the (key, value)
    pairs added."""
    blocks = []  # [key, value, lines]
    current = None
    for line in fresh_text.splitlines():
        s = line.strip()
        if s and not s.startswith('#') and '=' in s:
            key, value = s.split('=', 1)
            current = [key.strip(), value.strip(), [line.rstrip()]]
            blocks.append(current)
        elif current is not None and s.startswith('#'):
            current[2].append(line.rstrip())
        else:
            current = None  # a blank line (or the file header) ends a block
    existing = parse_server_properties(target)
    missing = [b for b in blocks if b[0] not in existing]
    if not missing or not target.exists():
        return []
    with open(target, 'r') as f:
        text = f.read()
    appended = "\n\n".join("\n".join(lines) for _, _, lines in missing)
    with open(target, 'w') as f:
        f.write(text.rstrip("\n") + "\n\n" + appended + "\n")
    return [(k, v) for k, v, _ in missing]


# ============================================================================
# BACKUP MANAGER
# ============================================================================

def _long_path(p) -> str:
    r"""An OS path safe to hand to shutil / zipfile / open on Windows.

    Absolute Windows paths get the \\?\ extended-length prefix, which lifts the
    legacy 260-char MAX_PATH limit (up to ~32767). Without it, backing up or
    restoring stock Bedrock's very deep resource_packs/chemistry tree fails with
    WinError 2/3 once the Server is installed under a longish path (and since
    Update backs up first, it then can't update either). No-op on POSIX and for
    paths already prefixed. copytree/rmtree given a \\?\ root propagate the
    prefix to every child, so deep descendants are covered too.
    """
    s = str(p)
    if sys.platform != "win32":
        return s
    if s.startswith("\\\\?\\"):
        return s
    ap = os.path.abspath(s)
    if ap.startswith("\\\\"):          # UNC \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


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
                with zipfile.ZipFile(_long_path(backup_path), 'w', zipfile.ZIP_DEFLATED) as zf:
                    for i, item in enumerate(preserve_items):
                        source = self.server_path / item
                        if source.exists():
                            if source.is_dir():
                                for file in source.rglob("*"):
                                    if file.is_file():
                                        arcname = str(file.relative_to(self.server_path))
                                        zf.write(_long_path(file), arcname)
                            else:
                                zf.write(_long_path(source), item)
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
                            shutil.copytree(_long_path(source), _long_path(dest))
                        else:
                            shutil.copy2(_long_path(source), _long_path(dest))
                        backed_up.append(item)
                    if progress_callback:
                        progress_callback((i + 1) / len(preserve_items) * 100)

            return True, backup_path, backed_up

        except Exception as e:
            # Cleanup failed backup
            if backup_path.exists():
                if backup_path.is_dir():
                    shutil.rmtree(_long_path(backup_path))
                else:
                    backup_path.unlink()
            raise e
    
    def restore_backup(self, backup_path: Path, progress_callback=None) -> Tuple[bool, List[str]]:
        restored = []
        
        try:
            if backup_path.suffix == '.zip':
                with zipfile.ZipFile(_long_path(backup_path), 'r') as zf:
                    members = zf.namelist()
                    for i, member in enumerate(members):
                        # Get top-level item name
                        top_level = member.split('/')[0]
                        if top_level not in restored:
                            dest = self.server_path / top_level
                            if dest.exists():
                                if dest.is_dir():
                                    shutil.rmtree(_long_path(dest))
                                else:
                                    dest.unlink()
                            restored.append(top_level)
                        zf.extract(member, _long_path(self.server_path))
                        if progress_callback and i % 50 == 0:
                            progress_callback((i + 1) / len(members) * 100)
            else:
                items = list(backup_path.iterdir())
                for i, item in enumerate(items):
                    dest = self.server_path / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(_long_path(dest))
                        else:
                            dest.unlink()
                    if item.is_dir():
                        shutil.copytree(_long_path(item), _long_path(dest))
                    else:
                        shutil.copy2(_long_path(item), _long_path(dest))
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
                        shutil.rmtree(_long_path(backup["path"]))
                    else:
                        backup["path"].unlink()
                    deleted += 1
                except Exception:
                    _LOG.exception("Could not delete old backup %s", backup["path"])

        return deleted

    def delete_backup(self, backup_path: Path) -> bool:
        try:
            if backup_path.is_dir():
                shutil.rmtree(_long_path(backup_path))
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
        self._failed_callbacks = set()  # ids of callbacks already logged as broken

    def add_output_callback(self, callback):
        self.output_callbacks.append(callback)
    
    def add_status_callback(self, callback):
        self.status_callbacks.append(callback)
    
    def _notify_output(self, line: str):
        for cb in self.output_callbacks:
            try:
                cb(line)
            except Exception:
                self._log_callback_failure(cb)

    def _notify_status(self, status: str):
        for cb in self.status_callbacks:
            try:
                cb(status)
            except Exception:
                self._log_callback_failure(cb)

    def _log_callback_failure(self, cb):
        # Output callbacks fire once per console line, so log each broken
        # callback once instead of flooding the log with the same traceback.
        if id(cb) not in self._failed_callbacks:
            self._failed_callbacks.add(id(cb))
            _LOG.exception("Server console callback failed (logged once per callback)")
    
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
            
            # Start the server process.
            # encoding/errors are explicit: BDS speaks UTF-8 on every platform,
            # but text=True alone would decode/encode with the process locale
            # (cp1252 on Windows) -- garbling Danish names/chat and, worse,
            # killing the stdout reader thread on bytes cp1252 can't decode
            # (many emoji), which would freeze the console and falsely report
            # the Server as stopped. errors="replace" guarantees the reader
            # never dies on odd bytes; UTF-8 also lets non-ASCII commands be sent.
            if sys.platform == "win32":
                self.process = subprocess.Popen(
                    [str(executable)],
                    cwd=str(self.server_path),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
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
                    encoding="utf-8",
                    errors="replace",
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
            # The console reader dying makes the GUI show the Server as stopped
            # even if the engine is still up -- record why.
            _LOG.exception("Server console reader stopped unexpectedly")
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
# SERVER SERVICE — headless per-Server operations
# ============================================================================
#
# One Server's operations with NO tkinter/widget dependency: process control,
# file reads/writes, players, gamerules, backups. Both the GUI (via the
# per-profile registry, self.contexts) and the remote-admin host / --agent
# operate through this same object, so an op triggered locally or remotely
# hits the identical code and (in-process) the identical ServerManager.
#
# Mutating process ops take a per-Server lock so two callers (e.g. the local
# GUI and a remote administrator, or two administrators) can't start/stop the
# same Server at once. File writes are last-write-wins (documented, family-scale).

class ServerService:
    def __init__(self, server_path: Path, config: dict, known_players: Optional[dict] = None):
        self.server_path = Path(server_path)
        self.config = config
        self.known_players = known_players if known_players is not None else {}
        self.server_manager = ServerManager(self.server_path)
        self.backup_manager = BackupManager(self.server_path, config)
        self.console_buffer = deque(maxlen=config.get("console_max_lines", 1000))
        self.server_manager.add_output_callback(self.console_buffer.append)
        self.server_manager.add_output_callback(self._note_engine_version)
        self._online: set = set()
        self._online_lock = threading.Lock()
        self.server_manager.add_output_callback(self._note_join_leave)
        self.server_manager.add_status_callback(self._clear_online_when_stopped)
        self._op_lock = threading.Lock()
        # Set while Update swaps this Server's files: a start then (from this
        # GUI or a remote Administrator) would run a half-extracted engine.
        self.update_in_progress = False

    def online_players(self) -> List[str]:
        """Names of the players on this Server right now (sorted)."""
        with self._online_lock:
            return sorted(self._online)

    def _note_join_leave(self, line: str):
        m = PLAYER_JOIN_LEAVE_RE.search(line)
        if m:
            with self._online_lock:
                if m.group(1).lower() == "connected":
                    self._online.add(m.group(2))
                else:
                    self._online.discard(m.group(2))

    def _clear_online_when_stopped(self, status: str):
        if status == "stopped":
            with self._online_lock:
                self._online.clear()

    def _note_engine_version(self, line: str):
        """The engine prints "Version: 1.26.52.3" at startup -- record it, so a
        Server installed before the app tracked versions stops showing Unknown."""
        m = _CONSOLE_VERSION_RE.search(line)
        if m and detect_server_version(self.server_path) != m.group(1):
            record_installed_version(self.server_path, m.group(1))

    # --- process control -------------------------------------------------
    def is_running(self) -> bool:
        return self.server_manager.is_running()

    def start(self) -> bool:
        with self._op_lock:
            if self.server_manager.is_running() or self.update_in_progress:
                return False
            return self.server_manager.start()

    def stop(self) -> bool:
        with self._op_lock:
            return self.server_manager.stop()

    def restart(self) -> bool:
        with self._op_lock:
            if self.update_in_progress:
                return False
            if self.server_manager.is_running():
                self.server_manager.stop()
                time.sleep(2)
            return self.server_manager.start()

    def send_command(self, command: str):
        self.server_manager.send_command(command)

    def console_snapshot(self) -> List[str]:
        return list(self.console_buffer)

    def server_port(self) -> str:
        return parse_server_properties(self.server_path / "server.properties").get("server-port", "19132")

    # --- reads -----------------------------------------------------------
    def get_info(self) -> dict:
        props = parse_server_properties(self.server_path)
        worlds = get_world_info(self.server_path)
        active = props.get("level-name", "")
        return {
            "name": props.get("server-name", self.server_path.name),
            "version": detect_server_version(self.server_path),
            "active_world": active,
            "gamemode": props.get("gamemode", "unknown"),
            "difficulty": props.get("difficulty", "unknown"),
            "max_players": props.get("max-players", "unknown"),
            "port": props.get("server-port", "19132"),
            "worlds_count": len(worlds),
            "worlds_size": format_size(get_folder_size(self.server_path / "worlds")),
            "running": self.is_running(),
            "platform": sys.platform,
            "valid": is_valid_bedrock_server(str(self.server_path))[0],
        }

    def list_worlds(self) -> List[dict]:
        return get_world_info(self.server_path)

    def read_properties(self) -> Dict[str, str]:
        return parse_server_properties(self.server_path / "server.properties")

    def get_active_world(self) -> str:
        return self.read_properties().get("level-name", "")

    def read_gamerules(self) -> dict:
        active = self.get_active_world()
        return read_world_gamerules(self.server_path / "worlds" / active)

    def rename_world(self, old_name: str, new_name: str) -> bool:
        old_dir = self.server_path / "worlds" / old_name
        new_dir = self.server_path / "worlds" / new_name
        if not old_dir.exists() or new_dir.exists():
            return False
        old_dir.rename(new_dir)
        if self.get_active_world() == old_name:
            self.set_active_world(new_name)
        return True

    def delete_world(self, name: str) -> bool:
        world_dir = self.server_path / "worlds" / name
        if not world_dir.exists():
            return False
        shutil.rmtree(world_dir)
        return True

    # --- player JSON helpers (allowlist.json / permissions.json) ---------
    def _load_player_json(self, filename: str) -> list:
        p = self.server_path / filename
        try:
            if p.exists():
                data = json.loads(p.read_text())
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _save_player_json(self, filename: str, entries: list):
        (self.server_path / filename).write_text(json.dumps(entries, indent=2))

    def get_players(self) -> dict:
        props = self.read_properties()
        return {
            "allowlist": self._load_player_json("allowlist.json"),
            "permissions": self._load_player_json("permissions.json"),
            "known_players": dict(self.known_players),
            "allow_list_enabled": props.get("allow-list", "false").lower() == "true",
            "force_gamemode": props.get("force-gamemode", "false").lower() == "true",
        }

    # --- writes ----------------------------------------------------------
    def write_properties(self, props: Dict[str, str]) -> bool:
        return save_server_properties(self.server_path / "server.properties", props)

    def set_active_world(self, name: str) -> bool:
        props_path = self.server_path / "server.properties"
        props = parse_server_properties(props_path)
        props["level-name"] = name
        return save_server_properties(props_path, props)

    def set_allowlist_enforcement(self, enable: bool) -> bool:
        props_path = self.server_path / "server.properties"
        props = parse_server_properties(props_path)
        props["allow-list"] = "true" if enable else "false"
        ok = save_server_properties(props_path, props)
        if self.is_running():
            self.send_command("allowlist " + ("on" if enable else "off"))
        return ok

    def add_allowlist_player(self, name: str, xuid: Optional[str] = None):
        entries = self._load_player_json("allowlist.json")
        if any(e.get("name", "").lower() == name.lower() for e in entries):
            return
        entry = {"ignoresPlayerLimit": False, "name": name}
        xuid = xuid or self.known_players.get(name)
        if xuid:
            entry["xuid"] = str(xuid)
        entries.append(entry)
        self._save_player_json("allowlist.json", entries)
        if self.is_running():
            self.send_command(f'allowlist add "{name}"')

    def remove_allowlist_player(self, name: str):
        entries = [e for e in self._load_player_json("allowlist.json") if e.get("name") != name]
        self._save_player_json("allowlist.json", entries)
        if self.is_running():
            self.send_command(f'allowlist remove "{name}"')

    def set_permission(self, xuid: str, level: str):
        entries = [e for e in self._load_player_json("permissions.json") if str(e.get("xuid")) != str(xuid)]
        entries.append({"permission": level, "xuid": str(xuid)})
        self._save_player_json("permissions.json", entries)
        if self.is_running():
            self.send_command("permission reload")

    def remove_permission(self, xuid: str):
        entries = [e for e in self._load_player_json("permissions.json") if str(e.get("xuid")) != str(xuid)]
        self._save_player_json("permissions.json", entries)
        if self.is_running():
            self.send_command("permission reload")

    def send_gamerule(self, rule: str, value: str):
        if not self.is_running():
            raise RuntimeError("Server must be running to change gamerules")
        self.send_command(f"gamerule {rule} {value}")

    def set_gamemode(self, name: str, mode: str):
        if not self.is_running():
            raise RuntimeError("Server must be running to set a player's game mode")
        self.send_command(f'gamemode {mode} "{name}"')

    # --- backups ---------------------------------------------------------
    def list_backups(self) -> List[dict]:
        return [{"name": b["name"], "date": b["date"], "size": b["size"], "path": str(b["path"])}
                for b in self.backup_manager.list_backups()]

    def create_backup(self, preserve_items: List[str], compress: bool = False, progress_callback=None):
        return self.backup_manager.create_backup(preserve_items, compress=compress,
                                                 progress_callback=progress_callback)

    def restore_backup(self, backup_path: Path, progress_callback=None):
        return self.backup_manager.restore_backup(Path(backup_path), progress_callback=progress_callback)

    def delete_backup(self, backup_path: Path) -> bool:
        return self.backup_manager.delete_backup(Path(backup_path))


# ============================================================================
# REMOTE ADMINISTRATION — WIRE PROTOCOL & AUTH
# ============================================================================
#
# LAN-only administration protocol (see docs/V2-MAJORDOMO-PLAN.md sections 2-3).
# One TCP connection per Machine, framed as JSON Lines: each message is a single
# json.dumps() object (ensure_ascii=True by default, so the payload is pure ASCII
# and can never contain a raw newline) terminated by b"\n".
#
# Security is honest LAN-grade: a high-entropy pairing token authenticates the
# client via an HMAC challenge (the token itself never crosses the wire), but the
# session is plaintext. Not internet-safe — documented as "don't port-forward this".

REMOTE_PROTO_VERSION = 1
REMOTE_DEFAULT_PORT = 19190
# Guard against a peer sending an unbounded line and exhausting memory. Backups/
# world lists are the largest legitimate payloads and stay well under this.
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
# Unambiguous alphabet for the pairing token (no 0/O/1/I/l to avoid mis-typing).
_TOKEN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


class ProtocolError(Exception):
    """A malformed or oversized message on the wire."""


def generate_pairing_token() -> str:
    """A high-entropy, human-copyable token shown on the host as XXXX-XXXX-XXXX."""
    groups = ["".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(4)) for _ in range(3)]
    return "-".join(groups)


def compute_auth(token: str, nonce: str) -> str:
    """HMAC-SHA256 of the host's challenge nonce, keyed by the pairing token.

    The client proves it knows the token without sending it; a fresh nonce per
    connection stops trivial replay. (A MITM on the plaintext LAN link could
    still hijack the live session — that's the accepted home-LAN threat model.)
    """
    return hmac.new(token.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_auth(token: str, nonce: str, response: str) -> bool:
    """Constant-time comparison of a client's auth response."""
    if not token or not isinstance(response, str):
        return False
    return hmac.compare_digest(compute_auth(token, nonce), response)


class FramedConnection:
    """JSON-Lines framing over a blocking socket.

    send_message() is serialized by a lock so multiple threads (the response
    writer and event pushers) can share one socket safely. recv_message()
    buffers across recv() calls so a message split across TCP segments — or
    several messages arriving in one segment — are handled correctly, and
    caps a single line at MAX_MESSAGE_BYTES. A socket read timeout propagates
    out as socket.timeout (buffer preserved), so callers can poll a stop flag.
    """

    def __init__(self, sock: socket.socket, max_message_bytes: int = MAX_MESSAGE_BYTES):
        self.sock = sock
        self.max_message_bytes = max_message_bytes
        self._buf = bytearray()
        self._send_lock = threading.Lock()

    def send_message(self, obj: dict):
        data = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            self.sock.sendall(data)

    def recv_message(self) -> Optional[dict]:
        """Next message as a dict, or None at clean EOF. Raises ProtocolError
        on an oversized or unparseable line; may raise socket.timeout."""
        while True:
            nl = self._buf.find(b"\n")
            if nl != -1:
                line = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line.decode("utf-8"))
                except Exception as e:
                    raise ProtocolError(f"unparseable message: {e}")
                if not isinstance(obj, dict):
                    raise ProtocolError("message was not a JSON object")
                return obj
            if len(self._buf) > self.max_message_bytes:
                raise ProtocolError("message exceeded size limit")
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self._buf.extend(chunk)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def remote_connect(host: str, port: int, token: str, timeout: float = 6.0):
    """Open and authenticate a client connection to a host (synchronous, linear).

    Returns (FramedConnection, machine_info dict) on success; raises RuntimeError
    with a human-readable message otherwise. This is the shared handshake used by
    both the "Test connection" button and MachineConnection's (re)connect path —
    it does NOT start any background thread.
    """
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except ConnectionRefusedError:
        raise RuntimeError(f"No host is accepting administration at {host}:{port}. "
                           "Is Remote Administration enabled there?")
    except (socket.timeout, OSError) as e:
        raise RuntimeError(f"Could not reach {host}:{port} ({e}).")
    sock.settimeout(timeout)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        pass
    conn = FramedConnection(sock)
    try:
        conn.send_message({"type": "hello", "proto": REMOTE_PROTO_VERSION, "app": APP_VERSION})
        challenge = conn.recv_message()
        if not challenge or challenge.get("type") != "challenge":
            if challenge and challenge.get("type") == "error":
                raise RuntimeError(challenge.get("error", "the host refused the connection"))
            raise RuntimeError("unexpected response from host during handshake")
        conn.send_message({"type": "auth", "response": compute_auth(token, challenge["nonce"])})
        ok = conn.recv_message()
        if not ok or ok.get("type") != "ok":
            raise RuntimeError((ok or {}).get("error", "the pairing token was rejected"))
        return conn, ok.get("machine", {})
    except ProtocolError as e:
        conn.close()
        raise RuntimeError(f"protocol error: {e}")
    except (socket.timeout, OSError) as e:
        conn.close()
        raise RuntimeError(f"connection lost during handshake ({e})")
    except RuntimeError:
        conn.close()
        raise


# ============================================================================
# REMOTE ADMINISTRATION — HOST SERVICE
# ============================================================================
#
# Serves a Machine's Servers to authenticated administrators over the LAN. Runs
# inside the GUI process (Settings toggle) or as the whole process (--agent).
#
# Threading model (the part most prone to subtle bugs, so it's kept rigid):
#   * one accept thread, polling a 1s socket timeout so stop() is responsive;
#   * per connection, a READER thread and a WRITER thread with a Queue between
#     them — the writer is the ONLY thing that ever writes that socket, so
#     event pushes (from ServerManager reader threads) and op responses can't
#     interleave on the wire;
#   * long ops (stop/restart/backup/restore) run in their own worker thread so
#     they don't block the reader from handling more requests;
#   * mutating process ops are serialized by ServerService._op_lock.
# Events fan out to every authed client; console bursts are coalesced by the
# writer. A dead/sleeping peer is caught by SO_KEEPALIVE plus the read timeout.

# Ops that can block long enough to deserve their own worker thread.
_ASYNC_OPS = {"stop", "restart", "create_backup", "restore_backup"}


class RemoteAdminHost:
    def __init__(self, provider, port: int = REMOTE_DEFAULT_PORT, log=None):
        # provider must supply: remote_token(), remote_service(id),
        # remote_server_list(), remote_machine_info(). Both BedrockUpdaterApp
        # and the headless AgentApp implement these.
        self.provider = provider
        self.port = port
        self.log = log or (lambda msg: None)
        self._server_sock = None
        self._accept_thread = None
        self._running = False
        self._clients = set()
        self._clients_lock = threading.Lock()
        self._wired = set()
        self._wire_lock = threading.Lock()

    def is_running(self) -> bool:
        return self._running

    def start(self):
        if self._running:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", self.port))
        except OSError as e:
            sock.close()
            raise RuntimeError(f"Could not listen on port {self.port}: {e}")
        sock.listen(8)
        sock.settimeout(1.0)
        self._server_sock = sock
        self._running = True
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        self.log(f"Remote administration listening on port {self.port}")

    def stop(self):
        self._running = False
        if self._server_sock:
            try:
                self._server_sock.close()
            except Exception:
                pass
            self._server_sock = None
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            c.close()
        self.log("Remote administration stopped")

    def _accept_loop(self):
        while self._running:
            try:
                conn_sock, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn_sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
            _ClientConn(self, conn_sock, addr).start()

    # --- client set ------------------------------------------------------
    def _add_client(self, c):
        with self._clients_lock:
            self._clients.add(c)

    def _remove_client(self, c):
        with self._clients_lock:
            self._clients.discard(c)

    def _broadcast(self, message: dict):
        with self._clients_lock:
            clients = list(self._clients)
        for c in clients:
            c.enqueue(message)

    def watch_service(self, profile_id: str, service):
        """Forward this Server's console and starts/stops to Administrators.
        The host's own GUI calls it for every Server it builds, so a Server
        started at the host is reported even if no Administrator has asked
        about it yet (before, only Servers an Administrator had used were)."""
        self._wire_service(profile_id, service)

    def notify_servers_changed(self):
        """Tell every connected Administrator the Server list changed (a Server
        added, removed or renamed here) -- status changes already do this."""
        self._broadcast({"type": "event", "event": "servers_changed",
                         "data": {"servers": self.provider.remote_server_list()}})

    # --- event wiring (once per Server) ----------------------------------
    def _wire_service(self, profile_id: str, service):
        with self._wire_lock:
            if profile_id in self._wired:
                return
            self._wired.add(profile_id)
        service.server_manager.add_output_callback(
            lambda line, pid=profile_id: self._broadcast(
                {"type": "event", "event": "console", "server": pid, "data": {"lines": [line]}}))
        service.server_manager.add_status_callback(
            lambda status, pid=profile_id: self._on_service_status(pid, status))

    def _on_service_status(self, profile_id: str, status: str):
        self._broadcast({"type": "event", "event": "status", "server": profile_id,
                         "data": {"running": status == "running"}})
        self._broadcast({"type": "event", "event": "servers_changed",
                         "data": {"servers": self.provider.remote_server_list()}})

    # --- op dispatch (called from a client's reader/worker thread) -------
    def dispatch_op(self, op: str, server_id, params: dict, client) -> dict:
        if op == "list_servers":
            return {"servers": self.provider.remote_server_list()}
        if op == "machine_info":
            return self.provider.remote_machine_info()

        service = self.provider.remote_service(server_id) if server_id else None
        if service is None:
            raise ValueError(f"unknown server: {server_id!r}")
        self._wire_service(server_id, service)

        if op == "get_info":
            return service.get_info()
        if op == "list_worlds":
            return {"worlds": service.list_worlds()}
        if op == "read_properties":
            return {"properties": service.read_properties()}
        if op == "read_gamerules":
            return {"gamerules": service.read_gamerules()}
        if op == "get_players":
            return service.get_players()
        if op == "console_snapshot":
            return {"lines": service.console_snapshot()}
        if op == "start":
            return {"started": service.start()}
        if op == "stop":
            return {"stopped": service.stop()}
        if op == "restart":
            return {"restarted": service.restart()}
        if op == "send_command":
            service.send_command(params["command"])
            return {}
        if op == "set_active_world":
            return {"ok": service.set_active_world(params["name"])}
        if op == "rename_world":
            return {"ok": service.rename_world(params["old_name"], params["new_name"])}
        if op == "delete_world":
            return {"ok": service.delete_world(params["name"])}
        if op == "write_properties":
            return {"ok": service.write_properties(params["properties"])}
        if op == "set_allowlist_enforcement":
            return {"ok": service.set_allowlist_enforcement(bool(params["enable"]))}
        if op == "add_allowlist_player":
            service.add_allowlist_player(params["name"], params.get("xuid"))
            return {}
        if op == "remove_allowlist_player":
            service.remove_allowlist_player(params["name"])
            return {}
        if op == "set_permission":
            service.set_permission(params["xuid"], params["level"])
            return {}
        if op == "remove_permission":
            service.remove_permission(params["xuid"])
            return {}
        if op == "send_gamerule":
            service.send_gamerule(params["rule"], params["value"])
            return {}
        if op == "set_gamemode":
            service.set_gamemode(params["name"], params["mode"])
            return {}
        if op == "list_backups":
            return {"backups": service.list_backups()}
        if op == "delete_backup":
            return {"ok": service.delete_backup(params["path"])}
        if op == "create_backup":
            preserve = params.get("preserve")
            if preserve is None:
                raise ValueError("create_backup requires a 'preserve' list")
            ok, path, backed = service.create_backup(
                preserve, compress=bool(params.get("compress", False)),
                progress_callback=lambda pct: self._broadcast(
                    {"type": "event", "event": "progress", "server": server_id,
                     "data": {"op": "backup", "percent": pct}}))
            return {"ok": ok, "path": str(path), "backed_up": backed}
        if op == "restore_backup":
            ok, restored = service.restore_backup(
                params["path"],
                progress_callback=lambda pct: self._broadcast(
                    {"type": "event", "event": "progress", "server": server_id,
                     "data": {"op": "restore", "percent": pct}}))
            return {"ok": ok, "restored": restored}
        raise ValueError(f"unknown op: {op!r}")


class _ClientConn:
    """One administrator connection: handshake, then a reader thread + a writer
    thread joined by an outbound queue (the writer is the sole socket writer)."""

    def __init__(self, host: RemoteAdminHost, sock: socket.socket, addr):
        self.host = host
        self.addr = addr
        self.sock = sock
        self.conn = FramedConnection(sock)
        self._out = queue.Queue()
        self._alive = True

    def start(self):
        threading.Thread(target=self._read_loop, daemon=True).start()
        threading.Thread(target=self._write_loop, daemon=True).start()

    def enqueue(self, message: dict):
        if self._alive:
            self._out.put(message)

    def close(self):
        if not self._alive:
            return
        self._alive = False
        self._out.put(None)  # wake the writer so it can exit
        self.conn.close()

    def _read_loop(self):
        added = False
        try:
            if not self._handshake():
                return
            self.host._add_client(self)
            added = True
            # snapshot of the fleet on connect
            self.enqueue({"type": "event", "event": "servers_changed",
                          "data": {"servers": self.host.provider.remote_server_list()}})
            self.sock.settimeout(1.0)
            while self.host._running and self._alive:
                try:
                    msg = self.conn.recv_message()
                except socket.timeout:
                    continue
                except (ProtocolError, OSError):
                    break
                if msg is None:
                    break
                self._dispatch(msg)
        except Exception:
            # Any failure on this connection (incl. the socket being closed out
            # from under us during host.stop()) just tears it down quietly --
            # a per-connection thread must never spew an unhandled traceback.
            pass
        finally:
            self.close()
            if added:
                self.host._remove_client(self)

    def _write_loop(self):
        try:
            while self._alive:
                item = self._out.get()
                if item is None:
                    break
                batch = [item]
                try:
                    while True:
                        batch.append(self._out.get_nowait())
                except queue.Empty:
                    pass
                for out_msg in self._coalesce(batch):
                    if out_msg is None:
                        return
                    try:
                        self.conn.send_message(out_msg)
                    except OSError:
                        return
        finally:
            self._alive = False

    @staticmethod
    def _coalesce(items):
        """Merge consecutive console events for the same Server into one message
        so a burst of BDS output becomes a few frames, not hundreds."""
        merged = []
        for m in items:
            if (m is not None and m.get("event") == "console" and merged
                    and isinstance(merged[-1], dict)
                    and merged[-1].get("event") == "console"
                    and merged[-1].get("server") == m.get("server")):
                merged[-1]["data"]["lines"].extend(m["data"]["lines"])
            else:
                merged.append(m)
        return merged

    def _handshake(self) -> bool:
        self.sock.settimeout(10.0)
        try:
            hello = self.conn.recv_message()
            if not hello or hello.get("type") != "hello":
                return False
            if hello.get("proto") != REMOTE_PROTO_VERSION:
                self.conn.send_message({"type": "error", "error": "unsupported protocol version"})
                return False
            nonce = secrets.token_hex(16)
            self.conn.send_message({"type": "challenge", "nonce": nonce})
            auth = self.conn.recv_message()
            token = self.host.provider.remote_token()
            if not auth or auth.get("type") != "auth" or not verify_auth(token, nonce, auth.get("response")):
                time.sleep(1.0)  # throttle brute-forcing the token
                try:
                    self.conn.send_message({"type": "error", "error": "authentication failed"})
                except Exception:
                    pass
                return False
            self.conn.send_message({"type": "ok", "machine": self.host.provider.remote_machine_info()})
            return True
        except (socket.timeout, ProtocolError, OSError):
            return False

    def _dispatch(self, msg: dict):
        if msg.get("type") != "req":
            return
        req_id = msg.get("id")
        op = msg.get("op")
        params = msg.get("params") or {}
        server_id = msg.get("server")
        if op in _ASYNC_OPS:
            threading.Thread(target=self._run_op, args=(req_id, op, server_id, params), daemon=True).start()
        else:
            self._run_op(req_id, op, server_id, params)

    def _run_op(self, req_id, op, server_id, params):
        try:
            result = self.host.dispatch_op(op, server_id, params, self)
            self.enqueue({"type": "resp", "id": req_id, "ok": True, "result": result})
        except Exception as e:
            self.enqueue({"type": "resp", "id": req_id, "ok": False, "error": str(e)})


# ============================================================================
# REMOTE ADMINISTRATION — CLIENT (Machine connection)
# ============================================================================
#
# The administrator's side: one MachineConnection per remote Machine. A single
# worker thread owns the socket's whole life -- (re)connect via remote_connect,
# then read messages until the link dies, then back off and reconnect -- so
# there is never more than one thread reading a given socket. Callers send
# requests from any thread (usually the tkinter thread); FramedConnection's
# locked send makes that safe alongside the worker's reads.
#
# Correlation: request() registers a slot keyed by a monotonic id and blocks on
# an Event until the worker matches the response by id (or a timeout fires, or
# the link drops -- which fails every in-flight request so no caller ever hangs).
# Server-pushed events go to on_event; connect/disconnect to on_state. Both fire
# on the worker thread; the caller wraps them to marshal onto the UI thread.

_REQUEST_TIMEOUT = 12.0        # seconds a blocking request waits for its response
_READ_TIMEOUT = 1.0            # recv poll interval, so close()/shutdown is prompt
_RECONNECT_MIN = 1.0           # backoff floor after a dropped/refused connection
_RECONNECT_MAX = 15.0          # backoff ceiling
_HEARTBEAT_INTERVAL = 20.0     # if idle this long, probe the host (reuses list_servers)
_HEARTBEAT_DEAD = 50.0         # no bytes at all for this long => treat link as dead


class _PendingRequest:
    __slots__ = ("event", "result", "error")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


class MachineConnection:
    """Persistent, auto-reconnecting connection to one remote Machine.

    on_event(event_dict) and on_state(connected: bool, detail: str) are invoked
    on the worker thread -- the caller is responsible for marshaling them onto
    the UI thread (e.g. root.after)."""

    def __init__(self, machine: dict, on_event=None, on_state=None, log=None):
        self.machine = machine
        self.id = machine.get("id")
        self.name = machine.get("name") or machine.get("host")
        self.host = machine["host"]
        self.port = int(machine.get("port", REMOTE_DEFAULT_PORT))
        self.token = machine.get("token", "")
        self._on_event = on_event or (lambda ev: None)
        self._on_state = on_state or (lambda connected, detail: None)
        self._log = log or (lambda m: None)

        self._lock = threading.Lock()
        self._conn = None
        self._pending = {}
        self._id_counter = 1
        self._connected = False
        self._closed = False
        self._machine_info = {}
        self._wake = threading.Event()      # interrupts the backoff sleep on close()
        self._worker = None

    # --- lifecycle -------------------------------------------------------
    def start(self):
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name=f"machine-{self.name}", daemon=True)
        self._worker.start()

    def close(self):
        self._closed = True
        self._wake.set()
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn:
            conn.close()  # unblock the worker's recv
        self._fail_pending("connection closed")

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def machine_info(self) -> dict:
        with self._lock:
            return dict(self._machine_info)

    # --- requests --------------------------------------------------------
    def request(self, op: str, server=None, params: dict = None, timeout: float = _REQUEST_TIMEOUT) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("connection is closed")
            conn = self._conn
            if conn is None:
                raise RuntimeError(f"not connected to {self.name}")
            rid = self._id_counter
            self._id_counter += 1
            pending = _PendingRequest()
            self._pending[rid] = pending
        try:
            conn.send_message({"type": "req", "id": rid, "op": op, "server": server, "params": params or {}})
        except OSError as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise RuntimeError(f"failed to send '{op}': {e}")
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise TimeoutError(f"'{op}' timed out after {timeout:g}s")
        if pending.error is not None:
            raise RuntimeError(pending.error)
        return pending.result if pending.result is not None else {}

    def _fail_pending(self, reason: str):
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        for _rid, pending in items:
            pending.error = reason
            pending.event.set()

    # --- worker: connect -> read -> reconnect ----------------------------
    def _run(self):
        backoff = _RECONNECT_MIN
        while not self._closed:
            try:
                conn, info = remote_connect(self.host, self.port, self.token)
            except RuntimeError as e:
                self._notify_state(False, str(e))
                if self._wake.wait(backoff) or self._closed:
                    break
                backoff = min(backoff * 2, _RECONNECT_MAX)
                continue
            # connected
            with self._lock:
                self._conn = conn
                self._machine_info = info
                self._connected = True
            backoff = _RECONNECT_MIN
            self._notify_state(True, "connected")
            self._read_until_dead(conn)
            # link died
            with self._lock:
                self._conn = None
                self._connected = False
            self._fail_pending("connection lost")
            conn.close()
            if self._closed:
                break
            self._notify_state(False, "connection lost — reconnecting")
            if self._wake.wait(backoff):
                break
            backoff = min(backoff * 2, _RECONNECT_MAX)

    def _read_until_dead(self, conn: FramedConnection):
        try:
            conn.sock.settimeout(_READ_TIMEOUT)
        except OSError:
            return
        last_recv = time.monotonic()
        last_hb = time.monotonic()
        while not self._closed:
            try:
                msg = conn.recv_message()
            except socket.timeout:
                now = time.monotonic()
                if now - last_recv > _HEARTBEAT_DEAD:
                    break  # host went silent -> force reconnect
                if now - last_recv > _HEARTBEAT_INTERVAL and now - last_hb > _HEARTBEAT_INTERVAL:
                    last_hb = now
                    self._send_heartbeat(conn)
                continue
            except (ProtocolError, OSError):
                break
            if msg is None:
                break
            last_recv = time.monotonic()
            self._handle_message(msg)

    def _send_heartbeat(self, conn: FramedConnection):
        # Fire-and-forget: a real round-trip that keeps the link warm and, more
        # importantly, whose *response* refreshes last_recv. id 0 is never used
        # by request() (which starts at 1), so the reply is simply ignored.
        try:
            conn.send_message({"type": "req", "id": 0, "op": "list_servers", "server": None, "params": {}})
        except OSError:
            pass  # the read loop will notice the dead socket

    def _handle_message(self, msg: dict):
        mtype = msg.get("type")
        if mtype == "resp":
            rid = msg.get("id")
            with self._lock:
                pending = self._pending.pop(rid, None)
            if pending is not None:
                if msg.get("ok"):
                    pending.result = msg.get("result", {})
                else:
                    pending.error = msg.get("error", "remote error")
                pending.event.set()
        elif mtype == "event":
            try:
                self._on_event(msg)
            except Exception:
                _LOG.exception("Remote event handler failed")

    def _notify_state(self, connected: bool, detail: str):
        try:
            self._on_state(connected, detail)
        except Exception:
            _LOG.exception("Remote connection-state handler failed")


class RemoteServerAccess:
    """Presents one remote Server with the SAME method surface as ServerService,
    so the tabs can drive a local or a remote Server through one interface. Each
    call becomes a request over the Machine's connection.

    is_running() is served from a cached flag (updated by get_info() and by
    note_status(), which the event layer calls on a status event) so the UI can
    poll it freely without a round-trip per call. create_backup's progress
    arrives as host-pushed 'progress' events, not via progress_callback (which
    is accepted only for signature parity with ServerService)."""

    def __init__(self, connection: MachineConnection, server_id: str):
        self.conn = connection
        self.server_id = server_id
        self._running = False

    def _req(self, op, timeout=_REQUEST_TIMEOUT, **params):
        return self.conn.request(op, server=self.server_id, params=params, timeout=timeout)

    def note_status(self, running: bool):
        self._running = bool(running)

    def online_players(self):
        """Not reported over the wire (yet): None means unknown, not 'nobody'."""
        return None

    # --- process control -------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        return bool(self._req("start").get("started", False))

    def stop(self) -> bool:
        return bool(self._req("stop", timeout=45).get("stopped", False))

    def restart(self) -> bool:
        return bool(self._req("restart", timeout=45).get("restarted", False))

    def send_command(self, command: str):
        self._req("send_command", command=command)

    def console_snapshot(self) -> List[str]:
        return self._req("console_snapshot").get("lines", [])

    def server_port(self) -> str:
        return str(self.get_info().get("port", "19132"))

    # --- reads -----------------------------------------------------------
    def get_info(self) -> dict:
        info = self._req("get_info")
        self._running = bool(info.get("running"))
        return info

    def list_worlds(self) -> List[dict]:
        return self._req("list_worlds").get("worlds", [])

    def read_properties(self) -> Dict[str, str]:
        return self._req("read_properties").get("properties", {})

    def get_active_world(self) -> str:
        return self.read_properties().get("level-name", "")

    def read_gamerules(self) -> dict:
        return self._req("read_gamerules").get("gamerules", {})

    def get_players(self) -> dict:
        return self._req("get_players")

    # --- writes ----------------------------------------------------------
    def write_properties(self, props: Dict[str, str]) -> bool:
        return bool(self._req("write_properties", properties=props).get("ok", False))

    def set_active_world(self, name: str) -> bool:
        return bool(self._req("set_active_world", name=name).get("ok", False))

    def rename_world(self, old_name: str, new_name: str) -> bool:
        return bool(self._req("rename_world", old_name=old_name, new_name=new_name).get("ok", False))

    def delete_world(self, name: str) -> bool:
        return bool(self._req("delete_world", name=name).get("ok", False))

    def set_allowlist_enforcement(self, enable: bool) -> bool:
        return bool(self._req("set_allowlist_enforcement", enable=enable).get("ok", False))

    def add_allowlist_player(self, name: str, xuid=None):
        self._req("add_allowlist_player", name=name, xuid=xuid)

    def remove_allowlist_player(self, name: str):
        self._req("remove_allowlist_player", name=name)

    def set_permission(self, xuid: str, level: str):
        self._req("set_permission", xuid=xuid, level=level)

    def remove_permission(self, xuid: str):
        self._req("remove_permission", xuid=xuid)

    def send_gamerule(self, rule: str, value: str):
        self._req("send_gamerule", rule=rule, value=value)

    def set_gamemode(self, name: str, mode: str):
        self._req("set_gamemode", name=name, mode=mode)

    # --- backups ---------------------------------------------------------
    def list_backups(self) -> List[dict]:
        return self._req("list_backups").get("backups", [])

    def create_backup(self, preserve_items: List[str], compress: bool = False, progress_callback=None):
        r = self._req("create_backup", timeout=600, preserve=preserve_items, compress=compress)
        return r.get("ok", False), r.get("path", ""), r.get("backed_up", [])

    def restore_backup(self, backup_path, progress_callback=None):
        r = self._req("restore_backup", timeout=600, path=str(backup_path))
        return r.get("ok", False), r.get("restored", [])

    def delete_backup(self, backup_path) -> bool:
        return bool(self._req("delete_backup", path=str(backup_path)).get("ok", False))


# ============================================================================
# CRAFTY INTERFACE (4.0) — the Minecraft-style look and layout
# ============================================================================
#
# docs/V4-CRAFTY-INTERFACE-PLAN.md. Pure presentation: nothing here talks to a
# Server. Settings ▸ Interface picks one of three looks (`ui_look`):
#   "classic" -- today's interface, untouched;
#   "coat"    -- the Crafty skin on today's tab layout ("New coat of paint");
#   "layout"  -- the Crafty skin + the left-menu layout ("New layout").
# Internally: look = classic | crafty, layout = tabs | menu.

UI_LOOKS = ("classic", "coat", "layout")

# Base classes that also exist under --agent (no tkinter), so the class statements below import.
_TkFrameBaseEarly = ttk.Frame if TK_AVAILABLE else object
_TkCanvasBaseEarly = tk.Canvas if TK_AVAILABLE else object

CRAFTY = {
    "void": "#161617", "menu": "#313233", "pane": "#48494a", "line": "#3a3b3c",
    "ink": "#ffffff", "dim": "#b1b2b5", "bar": "#e6e8eb", "bar_ink": "#1e1e1f",
    "btn": "#d0d1d4", "btn_hi": "#ffffff", "btn_lo": "#58585a", "btn_hover": "#e3e4e6",
    "green": "#3c8527", "green_hi": "#6cc349", "green_lo": "#1d4d13",
    "off": "#5a5b5c", "field": "#1f1f20", "console": "#0f0f10",
    "warn": "#ffb341", "bad": "#ff6b5e", "info": "#8fc3ff", "black": "#000000",
}
# Classic's colours were picked for a light background; on the dark Crafty one
# they map to these (self.fg()).
CRAFTY_FG = {
    "gray": "#b1b2b5", "grey": "#b1b2b5", "red": "#ff6b5e", "#F44336": "#ff6b5e", "#C62828": "#ff6b5e",
    "green": "#6cc349", "#4CAF50": "#6cc349", "#2E7D32": "#6cc349", "#FF9800": "#ffb341",
    "#E65100": "#ffb341", "blue": "#8fc3ff", "#2196F3": "#8fc3ff",
}
# Pixel fonts render crisply at whole pixel sizes, so sizes are negative (= pixels).
CRAFTY_FONT_SPECS = {
    "small": ("Pixelify Sans", -14), "body": ("Pixelify Sans", -16), "bold": ("Pixelify Sans", -16, "bold"),
    "row": ("Pixelify Sans", -18), "title": ("Silkscreen", -18, "bold"), "big": ("Silkscreen", -24, "bold"),
    "console": ("Pixel Operator Mono", -16),
}
_CRAFTY_FONT_FALLBACK = {"Pixelify Sans": "TkDefaultFont", "Silkscreen": "TkDefaultFont",
                         "Pixel Operator Mono": "TkFixedFont"}
_CRAFTY_FONTS_LOADED: Optional[bool] = None
# The background the Crafty controls sit on: darker in New coat of paint (as mockup 1),
# the lighter settings-pane grey in New layout (as mockup 2). Set by _setup_crafty_styles.
CRAFTY_SURFACE = {"bg": "#48494a"}


def get_font_cache_dir() -> Path:
    """Where the embedded Crafty fonts are unpacked for this process to load.
    Beside the settings file, so a test's config override keeps its fonts too."""
    if _CONFIG_PATH_OVERRIDE:
        return Path(_CONFIG_PATH_OVERRIDE).parent / "fonts"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "bedrock-updater" / "fonts"
    return Path.home() / ".config" / "bedrock-updater" / "fonts"


def _load_font_private(path: Path) -> bool:
    """Make one font file usable by THIS process only -- nothing is installed."""
    try:
        if sys.platform == "win32":
            import ctypes
            FR_PRIVATE = 0x10
            return ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0) > 0
        if sys.platform.startswith("linux"):
            import ctypes
            fc = ctypes.CDLL("libfontconfig.so.1")
            fc.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            fc.FcConfigAppFontAddFile.restype = ctypes.c_int
            return bool(fc.FcConfigAppFontAddFile(None, str(path).encode()))
    except Exception:
        _LOG.exception("Couldn't load font %s", path)
    return False  # macOS and others: the Crafty looks use the system fonts


def load_crafty_fonts() -> bool:
    """Unpack the embedded pixel fonts (+ their licences) and load them privately.
    Done once per process; False means the Crafty looks fall back to system fonts."""
    global _CRAFTY_FONTS_LOADED
    if _CRAFTY_FONTS_LOADED is not None:
        return _CRAFTY_FONTS_LOADED
    ok = False
    try:
        d = get_font_cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        for name, b64 in _EMBEDDED_FONTS.items():
            data = base64.b64decode(b64)
            target = d / name
            if not target.exists() or target.read_bytes() != data:
                tmp = target.with_name(target.name + ".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, target)
        (d / "FONT-LICENSES.txt").write_text(_EMBEDDED_FONT_LICENSES, encoding="utf-8")
        ok = all([_load_font_private(d / name) for name in _EMBEDDED_FONTS])
    except Exception:
        _LOG.exception("Couldn't unpack the Crafty fonts")
    if not ok:
        _LOG.warning("Crafty fonts couldn't be loaded; the Crafty looks use the system fonts")
    _CRAFTY_FONTS_LOADED = ok
    return ok


def crafty_font(kind: str):
    spec = CRAFTY_FONT_SPECS[kind]
    if _CRAFTY_FONTS_LOADED:
        return spec
    return (_CRAFTY_FONT_FALLBACK[spec[0]],) + spec[1:]


# Hand-drawn 8×8 pixel icons for the menu layout (no game assets).
PIXEL_PALETTE = {
    "k": "#1e1e1f", "b": "#3d7bd6", "B": "#7fb3ff", "g": "#5da03a", "G": "#86d158", "d": "#8b5a2b", "D": "#6b4220",
    "s": "#c79a73", "h": "#4a2f1b", "w": "#ffffff", "e": "#3a63c8", "m": "#7a3e2b", "p": "#efe7d2", "c": "#a0702c",
    "C": "#c8913c", "y": "#f2d24b", "q": "#9a9a9a", "Q": "#d8d8d8", "n": "#123f3a", "N": "#2a8f7e", "a": "#a8ecd9",
    "o": "#0a1f1c",
}
PIXEL_ICONS = {
    "server":   ["........", ".kkkkkk.", ".kbBbbk.", ".kbbBbk.", ".kbbbbk.", ".kkkkkk.", "...kk...", "..kkkk.."],
    "worlds":   ["gGggGggG", "gggGgggg", "dgddgdgd", "dddddddd", "ddDddddD", "dddddDdd", "dDdddddd", "dddddddd"],
    "players":  ["hhhhhhhh", "hhhhhhhh", "hsssssss", "sssssssh", "swessews", "ssssssss", "ssmmmmss", "ssssssss"],
    "teleport": ["..nnnn..", ".nNNNNn.", "nNaaNNNn", "nNaoaNNn", "nNNaaNNn", "nNNNNNNn", ".nNNNNn.", "..nnnn.."],
    "config":   [".pppppp.", ".pkkkkp.", ".pppppp.", ".pkkkpp.", ".pppppp.", ".pkkkkp.", ".pppppp.", "........"],
    "backups":  ["........", ".cccccc.", ".cCCCCc.", ".kkkkkk.", ".ccyycc.", ".cCCCCc.", ".cccccc.", "........"],
    "update":   ["...gg...", "..gGgg..", ".gGgggg.", "gggggggg", "..gggg..", "..gggg..", "..gggg..", "........"],
    "settings": ["...qq...", ".q.qq.q.", "..qQqq..", "qqq..qqq", "qqq..qqq", "..qqqq..", ".q.qq.q.", "...qq..."],
    "console":  ["........", "wwwwwwww", "wkkwkkkw", "wwwwwwww", "wkkkkwww", "wwwwwwww", ".ww.....", "w......."],
    "machines": ["kkkk....", "kbbk....", "kbbkkkkk", "kkkkbbBk", "..kkbbbk", "..kkkkkk", ".kk..kk.", "kkkkkkkk"],
}


def pixel_icon(name: str, scale: int = 3):
    """A tk.PhotoImage of one 8×8 icon, scaled up without smoothing."""
    img = tk.PhotoImage(width=8, height=8)
    for y, row in enumerate(PIXEL_ICONS[name]):
        for x, ch in enumerate(row):
            colour = PIXEL_PALETTE.get(ch)
            if colour:
                img.put(colour, (x, y))
    return img.zoom(scale)


class ToggleRow(_TkFrameBaseEarly):
    """The Crafty stand-in for ttk.Checkbutton: Minecraft's I/O switch, with the
    same `variable` / `command` / `text` / `state` and the same invoke() /
    instate() / state() calls the app uses on Checkbuttons -- so every handler
    stays exactly as it is. Clicking the text toggles too, like a Checkbutton."""
    W, H, HL = 64, 34, 2     # switch size, plus the focus ring around it

    def __init__(self, parent, text: str = "", variable=None, command=None, state="normal", **kw):
        kw.pop("style", None)
        super().__init__(parent, class_="CraftyToggle", **kw)
        self._var = variable if variable is not None else tk.BooleanVar(value=False)
        self._command = command
        self._disabled = str(state) == "disabled"
        bg = CRAFTY_SURFACE["bg"]
        # The canvas is the switch plus its focus ring; the switch is drawn inside the
        # ring (offset HL) so the ring never covers its black outline.
        self._canvas = tk.Canvas(self, width=self.W + 2 * self.HL, height=self.H + 2 * self.HL, bg=bg,
                                 highlightthickness=0, bd=0, takefocus=1, cursor="hand2")
        self._canvas.bind("<FocusIn>", lambda e: self._draw())
        self._canvas.bind("<FocusOut>", lambda e: self._draw())
        self._canvas.pack(side=tk.LEFT)
        self._label = ttk.Label(self, text=text, cursor="hand2") if text else None
        if self._label:
            self._label.pack(side=tk.LEFT, padx=(8, 0))
            self._label.bind("<Button-1>", lambda e: self.invoke())
        self._canvas.bind("<Button-1>", lambda e: (self._canvas.focus_set(), self.invoke()))
        for key in ("<space>", "<Return>"):
            self._canvas.bind(key, lambda e: self.invoke())
        self._trace = self._var.trace_add("write", lambda *a: self._draw())
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._draw()

    def _on_destroy(self, event):
        if event.widget is self and self._trace:
            try:
                self._var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _on(self) -> bool:
        try:
            return bool(self._var.get())
        except tk.TclError:
            return False

    def _draw(self):
        """Minecraft's switch: black outline, a raised light knob, and the free
        cell grey with an O (off) or green with an I (on), drawn as pixels."""
        c, W, H, on, o = self._canvas, self.W, self.H, self._on(), self.HL
        try:
            c.delete("all")
        except tk.TclError:
            return
        x0, y0, x1, y1 = o, o, o + W, o + H
        R = c.create_rectangle
        try:
            focused = c.focus_get() is c
        except (KeyError, tk.TclError):
            focused = False
        if focused:
            R(0, 0, W + 2 * o, H + 2 * o, fill=CRAFTY["green_hi"], width=0)
        R(x0, y0, x1, y1, fill="#000000", width=0)                       # outline
        R(x0 + 2, y0 + 2, x1 - 2, y1 - 2, fill=CRAFTY["green"] if on else "#8c8c8c", width=0)
        kw = 28                                                           # the knob
        kx0 = x1 - 2 - kw if on else x0 + 2
        kx1 = kx0 + kw
        sep = kx0 - 2 if on else kx1
        R(sep, y0 + 2, sep + 2, y1 - 2, fill="#000000", width=0)          # knob | cell line
        R(kx0, y0 + 2, kx1, y1 - 2, fill=CRAFTY["btn"], width=0)
        R(kx0, y0 + 2, kx1, y0 + 4, fill="#ffffff", width=0)              # lit top edge
        R(kx0, y1 - 8, kx1, y1 - 2, fill="#8e8f91", width=0)              # shaded bottom: raised
        f0, f1 = (x0 + 2, sep) if on else (sep + 2, x1 - 2)
        cx, cy = (f0 + f1) // 2, (y0 + y1) // 2
        if on:
            R(cx - 1, cy - 7, cx + 2, cy + 7, fill="#ffffff", width=0)    # I
        else:
            R(cx - 6, cy - 7, cx + 6, cy + 7, fill="#1e1e1f", width=0)    # O: a 3-px ring
            R(cx - 3, cy - 4, cx + 3, cy + 4, fill="#8c8c8c", width=0)
        if self._disabled:
            R(0, 0, W + 2 * o, H + 2 * o, fill=CRAFTY_SURFACE["bg"], stipple="gray50", width=0)

    # --- the Checkbutton calls the app (and its tests) use -------------------
    def invoke(self):
        if self._disabled:
            return
        self._var.set(not self._on())
        if self._command:
            self._command()

    def instate(self, spec) -> bool:
        flags = {"selected": self._on(), "disabled": self._disabled}
        for s in ([spec] if isinstance(spec, str) else spec):
            want = not s.startswith("!")
            if flags.get(s.lstrip("!"), False) != want:
                return False
        return True

    def state(self, spec=None):
        if spec:
            for s in ([spec] if isinstance(spec, str) else spec):
                if s.lstrip("!") == "disabled":
                    self._set_disabled(not s.startswith("!"))
        return ("disabled",) if self._disabled else ()

    def _set_disabled(self, disabled: bool):
        self._disabled = disabled
        self._canvas.configure(cursor="" if disabled else "hand2", takefocus=0 if disabled else 1)
        if self._label:
            self._label.configure(foreground=CRAFTY["dim"] if disabled else CRAFTY["ink"])
        self._draw()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        if "state" in kw:
            self._set_disabled(str(kw.pop("state")) == "disabled")
        if "text" in kw:
            text = kw.pop("text")
            if self._label:
                self._label.configure(text=text)
        if "command" in kw:
            self._command = kw.pop("command")
        if "variable" in kw:
            self._var = kw.pop("variable")
            self._trace = self._var.trace_add("write", lambda *a: self._draw())
            self._draw()
        if kw:
            return super().configure(**kw)

    config = configure

    def cget(self, key):
        if key == "state":
            return "disabled" if self._disabled else "normal"
        if key == "text":
            return self._label.cget("text") if self._label else ""
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


class CraftyTab(_TkCanvasBaseEarly):
    """One tab of New coat of paint's tab row, drawn like mockup 1's stone buttons:
    a bevelled grey face (green when chosen) and white text WITH a dark shadow --
    ttk can't draw text shadows, so the tab row is drawn here instead."""
    FACES = {"stone": ("#8b8b8b", "#c6c6c6", "#373737"), "hover": ("#9a9a9a", "#d4d4d4", "#404040"),
             "chosen": ("#4f9a2f", "#8fd96a", "#24541a")}

    def __init__(self, parent, text: str, command, font, bg: str = "#1b1b1c"):
        from tkinter import font as _tkfont
        f = _tkfont.Font(font=font)
        self.w, self.h = f.measure(text) + 34, f.metrics("linespace") + 18
        super().__init__(parent, width=self.w, height=self.h, bg=bg, highlightthickness=0, bd=0,
                         takefocus=1, cursor="hand2")
        self.text, self.command, self.font = text, command, font
        self.chosen = self.hover = False
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        for key in ("<space>", "<Return>"):
            self.bind(key, lambda e: self.command())
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self._draw()

    def _set_hover(self, on: bool):
        self.hover = on
        self._draw()

    def set_chosen(self, on: bool):
        if on != self.chosen:
            self.chosen = on
            self._draw()

    def _draw(self):
        w, h, R = self.w, self.h, self.create_rectangle
        face, hi, lo = self.FACES["chosen" if self.chosen else "hover" if self.hover else "stone"]
        self.delete("all")
        try:
            focused = self.focus_get() is self
        except (KeyError, tk.TclError):
            focused = False
        R(0, 0, w, h, fill=CRAFTY["green_hi"] if focused else "#000000", width=0)
        R(2, 2, w - 2, h - 2, fill=face, width=0)
        R(2, 2, w - 2, 4, fill=hi, width=0)            # light top-left edge ...
        R(2, 2, 4, h - 2, fill=hi, width=0)
        R(2, h - 5, w - 2, h - 2, fill=lo, width=0)    # ... dark bottom-right edge
        R(w - 4, 2, w - 2, h - 2, fill=lo, width=0)
        cx, cy = w // 2, h // 2 - 1
        self.create_text(cx + 2, cy + 2, text=self.text, fill="#3f3f3f", font=self.font)   # the shadow
        self.create_text(cx, cy, text=self.text, fill="#ffffff", font=self.font)


class CraftyButton(_TkCanvasBaseEarly):
    """Every button of the Crafty looks, drawn like the main tabs: a black frame, a
    light top-left and a thick dark bottom-right bevel, and white text with a dark
    shadow (New coat of paint = mockup 1's stone buttons; New layout = mockup 2's
    light Bedrock buttons, dark text). Same options and calls the app uses on a
    ttk.Button -- text, command, style, width, image, compound, state, configure,
    cget, state(), instate(), invoke() -- so no handler changes."""
    PAD = {"Small": (10, 4), "Big": (20, 10), "": (14, 6)}

    def __init__(self, parent, text: str = "", command=None, style: str = "", width: int = 0, image=None,
                 compound: str = "", state="normal", coat: bool = True, **kw):
        self._bg = _crafty_bg_of(parent)
        self._class = kw.pop("class_", "CraftyButton")   # a canvas has no -class; see winfo_class
        super().__init__(parent, bg=self._bg, highlightthickness=0, bd=0, takefocus=1, cursor="hand2", **kw)
        self._text, self._command, self._style, self._width_chars = text, command, style or "", int(width or 0)
        self._image, self._compound, self._coat = image, compound, coat
        self._disabled = str(state) == "disabled"
        self._hover = self._pressed = False
        self._menu = None
        self.bind("<Enter>", lambda e: self._set(hover=True))
        self.bind("<Leave>", lambda e: self._set(hover=False, pressed=False))
        self.bind("<ButtonPress-1>", lambda e: self._set(pressed=True))
        self.bind("<ButtonRelease-1>", self._release)
        for key in ("<space>", "<Return>"):
            self.bind(key, lambda e: self.invoke())
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self.bind("<Configure>", lambda e: self._draw())
        self._measure()
        if self._disabled:
            self.state(["disabled"])

    # --- look -----------------------------------------------------------------
    def winfo_class(self):
        return self._class

    def _kind(self) -> str:
        st = self._style
        if "Danger" in st:
            return "danger"
        if "Primary" in st or "Chosen" in st:
            return "green"
        return "plain"

    def _faces(self):
        """(face, light edge, dark edge, text colour, shadow?) for the current state."""
        kind, coat = self._kind(), self._coat
        if self._disabled:
            if kind == "green":
                return "#5d7a54", "#7d9a72", "#3c5636", "#c9d6c4", False
            return ("#5f5f5f", "#7a7a7a", "#3a3a3a", "#a8a8a8", False) if coat else \
                   ("#8e8f91", "#a9aaac", "#6a6b6d", "#55565a", False)
        if kind == "danger":
            face = ("#b3362c", "#e06a5f", "#6e1c16", "#ffffff", True)
        elif kind == "green":
            face = ("#4f9a2f", "#8fd96a", "#24541a", "#ffffff", True) if coat else \
                   (CRAFTY["green"], CRAFTY["green_hi"], CRAFTY["green_lo"], "#ffffff", True)
        else:
            face = ("#8b8b8b", "#c6c6c6", "#373737", "#ffffff", True) if coat else \
                   (CRAFTY["btn"], "#ffffff", CRAFTY["btn_lo"], CRAFTY["bar_ink"], False)
        if self._hover:
            face = (_crafty_mix(face[0], "#ffffff", 0.12),) + face[1:]
        return face

    def _font(self):
        st = self._style
        if "Small" in st:
            return crafty_font("small")
        if "Big" in st:
            return crafty_font("row")
        return crafty_font("bold") if "Primary" in st else crafty_font("body")

    def _measure(self):
        from tkinter import font as _tkfont
        f = _tkfont.Font(font=self._font())
        px, py = self.PAD["Small" if "Small" in self._style else "Big" if "Big" in self._style else ""]
        tw = f.measure(self._text) if self._text else 0
        self._tw = tw
        self._shadow_runs = _shadow_runs(self._text, f.measure)
        iw = self._image.width() if self._image else 0
        ih = self._image.height() if self._image else 0
        gap = 8 if (self._image and self._text) else 0
        w = max(tw + iw + gap + 2 * px + 6, self._width_chars * f.measure("0") + 2 * px)
        h = max(f.metrics("linespace"), ih) + 2 * py + 7
        self.configure_canvas(width=max(w, 24), height=h)
        self._draw()

    def configure_canvas(self, **kw):
        tk.Canvas.configure(self, **kw)

    def _draw(self):
        try:
            self.delete("all")
        except tk.TclError:
            return
        w = max(self.winfo_width(), int(tk.Canvas.cget(self, "width")))
        h = max(self.winfo_height(), int(tk.Canvas.cget(self, "height")))
        if self.winfo_width() > 1:
            w, h = self.winfo_width(), self.winfo_height()
        face, hi, lo, ink, shadow = self._faces()
        if self._pressed and not self._disabled:
            hi, lo = lo, hi
        R = self.create_rectangle
        try:
            focused = self.focus_get() is self
        except (KeyError, tk.TclError):
            focused = False
        R(0, 0, w, h, fill=CRAFTY["green_hi"] if focused else "#000000", width=0)
        R(2, 2, w - 2, h - 2, fill=face, width=0)
        R(2, 2, w - 2, 4, fill=hi, width=0)            # light top-left edge
        R(2, 2, 4, h - 2, fill=hi, width=0)
        R(2, h - 5, w - 2, h - 2, fill=lo, width=0)    # thick dark bottom-right edge
        R(w - 4, 2, w - 2, h - 2, fill=lo, width=0)
        self._draw_extra(w, h)
        shift = 1 if self._pressed and not self._disabled else 0
        tw = self._tw
        iw = self._image.width() if self._image else 0
        gap = 8 if (self._image and self._text) else 0
        x = (w - (tw + iw + gap)) // 2 + shift
        cy = (h - 3) // 2 + shift
        if self._image:
            self.create_image(x, cy, image=self._image, anchor="w")
            x += iw + gap
        if self._text:
            if shadow:   # the shadow, under the letters only (a colour emoji gets none)
                for dx, run in self._shadow_runs:
                    self.create_text(x + dx + 2, cy + 2, text=run, fill="#3f3f3f", font=self._font(), anchor="w")
            self.create_text(x, cy, text=self._text, fill=ink, font=self._font(), anchor="w")

    def _draw_extra(self, w, h):
        pass

    def _set(self, hover=None, pressed=None):
        if hover is not None:
            self._hover = hover and not self._disabled
        if pressed is not None:
            self._pressed = pressed and not self._disabled
        self._draw()

    def _release(self, event):
        was = self._pressed
        self._set(pressed=False)
        if was and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.invoke()

    # --- the ttk.Button calls the app uses -------------------------------------------
    def invoke(self):
        if self._disabled:
            return None
        if self._menu is not None:
            self._menu.post(self.winfo_rootx(), self.winfo_rooty() + self.winfo_height())
            return None
        return self._command() if self._command else None

    def instate(self, spec) -> bool:
        flags = {"disabled": self._disabled, "pressed": self._pressed, "selected": False}
        for s in ([spec] if isinstance(spec, str) else spec):
            if flags.get(s.lstrip("!"), False) != (not s.startswith("!")):
                return False
        return True

    def state(self, spec=None):
        if spec:
            for s in ([spec] if isinstance(spec, str) else spec):
                if s.lstrip("!") == "disabled":
                    self._disabled = not s.startswith("!")
            self.configure_canvas(cursor="" if self._disabled else "hand2", takefocus=0 if self._disabled else 1)
            self._hover = False
            self._draw()
        return ("disabled",) if self._disabled else ()

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        remeasure = False
        for key in ("text", "style", "image", "compound", "width"):
            if key in kw:
                v = kw.pop(key)
                if key == "width":
                    self._width_chars = int(v or 0)
                else:
                    setattr(self, "_" + key, v if key != "style" else (v or ""))
                remeasure = True
        if "command" in kw:
            self._command = kw.pop("command")
        if "menu" in kw:
            self._menu = kw.pop("menu")
        if "state" in kw:
            self.state(["disabled"] if str(kw.pop("state")) == "disabled" else ["!disabled"])
        if kw:
            tk.Canvas.configure(self, **kw)
        if remeasure:
            self._measure()

    config = configure

    def cget(self, key):
        if key in ("text", "style", "image", "compound", "command"):
            return getattr(self, "_" + key)
        if key == "state":
            return "disabled" if self._disabled else "normal"
        if key == "width":
            return self._width_chars
        return tk.Canvas.cget(self, key)

    def __getitem__(self, key):
        return self.cget(key)

    def __setitem__(self, key, value):
        self.configure(**{key: value})


class CraftyRadio(CraftyButton):
    """A Crafty button that is one of a group (the Server strip, the Interface row):
    `variable` / `value` / `command` like ttk.Radiobutton. kind="strip" = a dark
    button with a GREEN OUTLINE when chosen (so the running dot stays visible);
    kind="choice" = a normal button that turns green when chosen."""

    def __init__(self, parent, variable=None, value="", kind: str = "choice", **kw):
        self._var, self._value, self._radio_kind = variable, value, kind
        super().__init__(parent, class_="CraftyRadio", **kw)
        self._trace = self._var.trace_add("write", lambda *a: self._draw()) if self._var is not None else None
        self.bind("<Destroy>", self._untrace, add="+")

    def _untrace(self, event):
        if event.widget is self and self._trace:
            try:
                self._var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            self._trace = None

    def _chosen(self) -> bool:
        try:
            return self._var is not None and self._var.get() == self._value
        except tk.TclError:
            return False

    def _kind(self) -> str:
        if self._radio_kind == "choice" and self._chosen():
            return "green"
        return "strip" if self._radio_kind == "strip" else "plain"

    def _faces(self):
        if self._radio_kind == "strip" and not self._disabled:
            face = (CRAFTY["pane"] if self._chosen() else CRAFTY["menu"], "#4a4b4c", "#1f1f20", "#ffffff", True)
            if self._hover:
                face = (_crafty_mix(face[0], "#ffffff", 0.08),) + face[1:]
            return face
        return super()._faces()

    def _draw_extra(self, w, h):
        if self._radio_kind == "strip" and self._chosen():
            g = CRAFTY["green_hi"]
            for x0, y0, x1, y1 in ((2, 2, w - 2, 4), (2, h - 4, w - 2, h - 2), (2, 2, 4, h - 2), (w - 4, 2, w - 2, h - 2)):
                self.create_rectangle(x0, y0, x1, y1, fill=g, width=0)

    def invoke(self):
        if self._disabled:
            return None
        if self._var is not None:
            self._var.set(self._value)
        return self._command() if self._command else None

    def instate(self, spec) -> bool:
        flags = {"disabled": self._disabled, "selected": self._chosen()}
        for s in ([spec] if isinstance(spec, str) else spec):
            if flags.get(s.lstrip("!"), False) != (not s.startswith("!")):
                return False
        return True


def _shadow_runs(text: str, measure) -> list:
    """(x offset, run) for the parts of a label that get a text shadow: everything
    but colour emoji (U+1Fxxx, or a symbol asked for as emoji with U+FE0F)."""
    runs, start, i = [], 0, 0
    def emoji_at(j):
        c = text[j]
        return ord(c) >= 0x1F000 or c in "\ufe0f\u200d" or (j + 1 < len(text) and text[j + 1] == "\ufe0f")
    while i <= len(text):
        if i == len(text) or emoji_at(i):
            if i > start and text[start:i].strip():
                runs.append((measure(text[:start]) if start else 0, text[start:i]))
            start = i + 1
        i += 1
    return runs


def _crafty_mix(colour: str, other: str, t: float) -> str:
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def _crafty_bg_of(parent) -> str:
    """The colour behind a widget: its parent's own background (a tk widget's bg,
    or the parent's ttk style), falling back to the look's surface."""
    try:
        return parent.cget("bg")
    except tk.TclError:
        pass
    try:
        style = parent.cget("style") or parent.winfo_class()
        found = ttk.Style().lookup(style, "background")
        if found:
            return found
    except tk.TclError:
        pass
    return CRAFTY_SURFACE["bg"]


class CraftySlider(_TkCanvasBaseEarly):
    """A green-filled Minecraft slider (canvas) for whole numbers.
    get() returns the value as text, so it fits the properties editor's map."""

    def __init__(self, parent, minimum: int, maximum: int, value: int, command=None, width: int = 360, **kw):
        bg = CRAFTY_SURFACE["bg"]
        super().__init__(parent, width=width, height=36, bg=bg, highlightthickness=0, bd=0,
                         takefocus=1, cursor="hand2", **kw)
        self.lo, self.hi = int(minimum), max(int(maximum), int(minimum) + 1)
        self.value = min(max(int(value), self.lo), self.hi)
        self.command = command
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<Left>", lambda e: self.set(self.value - 1))
        self.bind("<Right>", lambda e: self.set(self.value + 1))
        self.bind("<FocusIn>", lambda e: self._draw())
        self.bind("<FocusOut>", lambda e: self._draw())
        self._draw()

    def _geometry(self):
        w = max(self.winfo_width(), int(self.cget("width")))
        return 2, w - 2

    def _draw(self):
        """Black-outlined track, green up to the thumb; a raised light thumb.
        Everything sits 2 px inside the canvas so nothing is clipped."""
        x0, x1 = self._geometry()
        frac = (self.value - self.lo) / (self.hi - self.lo)
        kx = x0 + frac * (x1 - x0 - 22)
        R = self.create_rectangle
        self.delete("all")
        try:
            focused = self.focus_get() is self
        except (KeyError, tk.TclError):
            focused = False
        R(x0, 10, x1, 26, fill="#000000", width=0)                        # track outline
        R(x0 + 2, 12, kx + 11, 24, fill=CRAFTY["green"], width=0)
        R(x0 + 2, 12, kx + 11, 14, fill=CRAFTY["green_hi"], width=0)
        R(kx + 11, 12, x1 - 2, 24, fill="#2a2a2b", width=0)
        R(kx, 2, kx + 22, 34, fill=CRAFTY["green_hi"] if focused else "#000000", width=0)
        R(kx + 2, 4, kx + 20, 32, fill=CRAFTY["btn"], width=0)              # thumb
        R(kx + 2, 4, kx + 20, 6, fill="#ffffff", width=0)
        R(kx + 2, 26, kx + 20, 32, fill="#8e8f91", width=0)

    def _drag(self, event):
        self.focus_set()
        x0, x1 = self._geometry()
        frac = (event.x - x0 - 10) / max(1, (x1 - x0 - 20))
        self.set(round(self.lo + min(max(frac, 0.0), 1.0) * (self.hi - self.lo)))

    def set(self, value: int):
        value = min(max(int(value), self.lo), self.hi)
        if value != self.value:
            self.value = value
            self._draw()
            if self.command:
                self.command(value)

    def get(self) -> str:
        return str(self.value)


# Configuration in the menu layout: plain titles, a line of explanation, a group
# and a control for every known server.properties key. Unknown keys (a future
# Mojang setting) still show, under "Other", with their own name -- nothing is
# ever hidden. kind: bool | slider (lo, hi[, unit]) | choice ([values]) | text.
PROPERTY_GROUPS = [
    ("Players", "Who can join and what they may do"), ("World", "How the World plays"),
    ("Network", "How players reach this Server"), ("Extras", "Settings Mojang's file leaves out"),
    ("Advanced", "Anti-cheat, performance and debugging — leave these unless you know why"),
    ("Other", "Settings this version of the app doesn't know yet"),
]
PROPERTY_INFO = {
    "server-name": ("Network", "Name players see", "In their server list", "text", None),
    "allow-cheats": ("Players", "Allow cheats", "Players with the Operator role can use commands like /tp. "
                     "Turning it on ends achievements for this World for good.", "bool", None),
    "online-mode": ("Players", "Online mode", "Players must be signed in to Xbox Live", "bool", None),
    "allow-list": ("Players", "Only players on the list may join", "The allowlist on 👥 Players", "bool", None),
    "max-players": ("Players", "Max players", "How many can play at once", "slider", (1, 30, "")),
    "default-player-permission-level": ("Players", "Role for new players", "What a first-time player may do",
                                        "choice", ["visitor", "member", "operator"]),
    "gamemode": ("Players", "Game mode", "For new players", "choice", ["survival", "creative", "adventure"]),
    "force-gamemode": ("Players", "Force game mode", "Everyone gets the game mode above on every join", "bool", None),
    "player-idle-timeout": ("Players", "Kick idle players after", "Minutes of doing nothing; 0 = never",
                            "slider", (0, 60, " min")),
    "chat-restriction": ("Players", "Chat", "None = free chat · Dropped = messages are dropped · "
                         "Disabled = no chat for non-operators", "choice", ["None", "Dropped", "Disabled"]),
    "disable-player-interaction": ("Players", "Players ignore each other", "A hint to devices, not enforced", "bool", None),
    "disable-custom-skins": ("Players", "Block custom skins", "Only store and in-game skins", "bool", None),
    "difficulty": ("World", "Difficulty", "", "choice", ["peaceful", "easy", "normal", "hard"]),
    "level-seed": ("World", "Seed for new Worlds", "Used only when a World is generated", "text", None),
    "view-distance": ("World", "View distance", "How far players can see", "slider", (5, 64, " chunks")),
    "tick-distance": ("World", "Simulation distance", "How far from players the World keeps moving",
                      "slider", (4, 12, " chunks")),
    "texturepack-required": ("World", "Require the World's texture packs", "", "bool", None),
    "server-port": ("Network", "Port", "Players add the Server with this computer's address and this port", "text", None),
    "server-portv6": ("Network", "Port (IPv6)", "", "text", None),
    "enable-lan-visibility": ("Network", "Show in Minecraft's LAN list",
                              "Only one Server per computer is listed at a time: the one started first", "bool", None),
    "transport": ("Network", "Transport", "NetherNet is what current Minecraft uses", "choice", ["nethernet", "raknet"]),
    "server-ip": ("Network", "Listen on address", "Empty = every network card", "text", None),
    "emit-server-telemetry": ("Extras", "Send server telemetry to Mojang", "Anonymous gameplay statistics", "bool", None),
    "disable-client-vibrant-visuals": ("Extras", "Turn off Vibrant Visuals for players",
                                       "Untick to let players' devices use Minecraft's new graphics", "bool", None),
    "chat-logging-enabled": ("Extras", "Save players' chat to a log file (experimental)",
                             "Bedrock 1.26.52 doesn't write the log yet", "bool", None),
    "max-threads": ("Advanced", "Max threads", "0 = as many as the computer has", "text", None),
    "compression-threshold": ("Advanced", "Compression threshold", "", "text", None),
    "compression-algorithm": ("Advanced", "Compression", "", "choice", ["zlib", "snappy"]),
    "server-authoritative-movement-strict": ("Advanced", "Strict movement checks", "", "bool", None),
    "server-authoritative-dismount-strict": ("Advanced", "Strict dismount checks", "", "bool", None),
    "server-authoritative-entity-interactions-strict": ("Advanced", "Strict interaction checks", "", "bool", None),
    "server-authoritative-block-breaking": ("Advanced", "Server checks block breaking", "", "bool", None),
    "player-position-acceptance-threshold": ("Advanced", "Position tolerance", "", "text", None),
    "player-movement-action-direction-threshold": ("Advanced", "Attack direction tolerance", "", "text", None),
    "server-authoritative-block-breaking-pick-range-scalar": ("Advanced", "Block breaking range", "", "text", None),
    "client-side-chunk-generation-enabled": ("Advanced", "Devices build far-away chunks", "", "bool", None),
    "block-network-ids-are-hashes": ("Advanced", "Stable block IDs", "", "bool", None),
    "server-build-radius-ratio": ("Advanced", "Server build radius", "Disabled = automatic", "text", None),
    "disable-persona": ("Advanced", "Disable persona (Mojang: internal use only)", "", "bool", None),
    "content-log-file-enabled": ("Advanced", "Log add-on errors to a file", "", "bool", None),
    "content-log-console-output-enabled": ("Advanced", "Show add-on errors in the console", "", "bool", None),
    "content-log-level": ("Advanced", "Add-on log level", "", "choice", ["error", "warning", "info", "verbose"]),
    "allow-outbound-script-debugging": ("Advanced", "Script debugger: connect out", "", "bool", None),
    "allow-inbound-script-debugging": ("Advanced", "Script debugger: listen", "", "bool", None),
    "script-debugger-auto-attach": ("Advanced", "Script debugger: auto attach", "", "choice",
                                    ["disabled", "connect", "listen"]),
}


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
        self.contexts: Dict[str, "ServerService"] = {}
        # In-process remote-administration host (Settings > Remote Administration).
        self.remote_host: Optional[RemoteAdminHost] = None
        # Outgoing connections to remote Machines (Stage 3): machine_id -> MachineConnection.
        # _remote_state caches each Machine's last-known {connected, servers} for the sidebar.
        self.connections: Dict[str, MachineConnection] = {}
        self._remote_state: Dict[str, dict] = {}
        # MachineConnection callbacks fire on their worker thread; tkinter is not
        # thread-safe (root.after() from a worker raises "main thread is not in
        # main loop" on modern Python), so workers only enqueue here and the
        # MAIN thread drains it via a self-scheduled poll -- the robust pattern.
        self._remote_queue: "queue.Queue" = queue.Queue()
        # Servers (by access object) already checked this session for the
        # "not set to NetherNet" console warning, so the fix is offered once.
        self._transport_checked: set = set()
        # Update tab: latest release from Mojang (refresh_update_status) and the
        # selected Server's installed version (set by _apply_server_info).
        self._latest_release: Optional[Dict[str, str]] = None
        self._latest_error: Optional[str] = None
        self._latest_checked_at = 0.0
        self._latest_check_running = False
        self._announce_latest = False  # log the result of the startup check once
        self._installed_version = "—"
        self._server_folder_state = "none"  # "valid" | "empty" | "invalid" | "none"
        # The Server currently shown in the tabs, as a uniform access object
        # (a local ServerService or a RemoteServerAccess). active_remote is
        # (machine_id, server_id) when remote, else None.
        self.active_access = None
        self.active_remote = None

        self.setup_logging()
        for problem in _CONFIG_LOAD_PROBLEMS:
            self.logger.error(problem)  # load_config ran before logging existed
        self._init_look()
        self.setup_styles()
        self.setup_ui()
        self.apply_theme()
        self.load_saved_state()
        self.setup_keyboard_shortcuts()
        self.root.bind_class("Toplevel", "<Map>", self._on_toplevel_map, add="+")
        if _CONFIG_LOAD_PROBLEMS:
            self.root.after(300, lambda: messagebox.showwarning(
                "Settings could not be read", "\n\n".join(_CONFIG_LOAD_PROBLEMS)))

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Resume hosting if it was left enabled last session.
        if self.config.get("remote_admin", {}).get("enabled"):
            self.root.after(500, lambda: self.toggle_remote_admin(startup=True))

        # Main-thread poller that drains remote-connection events safely.
        self.root.after(200, self._drain_remote_queue)
        # Open connections to any configured remote Machines.
        self.root.after(600, self._ensure_connections)
        
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
            handlers=[logging.FileHandler(log_file, encoding='utf-8')]
        )
        self.logger = logging.getLogger(__name__)
        # Uncaught exceptions in button handlers and worker threads used to go
        # only to stderr -- lost when the app is started from a desktop icon.
        self.root.report_callback_exception = self._log_callback_exception
        threading.excepthook = self._log_thread_exception

    def _log_callback_exception(self, exc_type, exc, tb):
        self.logger.error("Unhandled error in the GUI", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def _log_thread_exception(self, args):
        if args.exc_type is SystemExit:
            return
        name = args.thread.name if args.thread else "?"
        self.logger.error("Unhandled error in thread %s", name,
                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
        threading.__excepthook__(args)
            
    def setup_styles(self):
        self.style = ttk.Style()
        if not hasattr(self, "_classic_base_theme"):
            self._classic_base_theme = self.style.theme_use()
        if getattr(self, "look", "classic") == "crafty":
            self._setup_crafty_styles()
            return
        self.style.configure("Success.TButton", foreground=self.fg("green"))
        self.style.configure("Danger.TButton", foreground=self.fg("red"))
        self.style.configure("Primary.TButton", font=self.font(10, "bold"))
        # Green step arrows and compact secondary buttons for the Update tab.
        self.style.configure("Arrow.TLabel", foreground=self.fg("#4CAF50"), font=self.font(13, "bold"))
        self.style.configure("Small.TButton", font=self.font(8))

    # --- Look and layout (4.0, docs/V4-CRAFTY-INTERFACE-PLAN.md) -----------
    def _init_look(self):
        """Read Settings ▸ Interface: look = classic | crafty, layout = tabs | menu."""
        look = self.config.get("ui_look", "classic")
        self.ui_look = look if look in UI_LOOKS else "classic"
        self.look = "classic" if self.ui_look == "classic" else "crafty"
        self.layout = "menu" if self.ui_look == "layout" else "tabs"
        if self.look == "crafty":
            load_crafty_fonts()

    def fg(self, colour: str) -> str:
        """A text colour, as Classic has always used it, or mapped for the dark Crafty look."""
        if getattr(self, "look", "classic") == "crafty":
            return CRAFTY_FG.get(colour, colour)
        return colour

    def font(self, size: int, weight: Optional[str] = None):
        """Classic's TkDefaultFont at a size, or the Crafty pixel font closest to it."""
        if getattr(self, "look", "classic") != "crafty":
            return ("TkDefaultFont", size, weight) if weight else ("TkDefaultFont", size)
        if size >= 12:
            return crafty_font("big") if size >= 14 else crafty_font("title")
        if size <= 8:
            return crafty_font("small")
        return crafty_font("bold") if weight else crafty_font("body")

    def check(self, parent, **kw):
        """An on/off control: a Checkbutton in Classic, Minecraft's I/O switch in Crafty."""
        if getattr(self, "look", "classic") == "crafty":
            return ToggleRow(parent, **kw)
        return ttk.Checkbutton(parent, **kw)

    def button(self, parent, **kw):
        """A push button: ttk's in Classic, a drawn one (bevel + text shadow) in Crafty."""
        if getattr(self, "look", "classic") == "crafty":
            return CraftyButton(parent, coat=self.layout == "tabs", **kw)
        return ttk.Button(parent, **kw)

    def choice_button(self, parent, variable, value, kind: str = "choice", **kw):
        """One of a group of buttons (Server strip, Interface row): a Toolbutton-style
        ttk.Radiobutton in Classic, a drawn CraftyRadio in Crafty."""
        if getattr(self, "look", "classic") == "crafty":
            return CraftyRadio(parent, variable=variable, value=value, kind=kind, coat=self.layout == "tabs", **kw)
        return ttk.Radiobutton(parent, variable=variable, value=value, style="Toolbutton", **kw)

    def _setup_crafty_styles(self):
        """The Crafty skin as its own ttk theme (so Classic's is never touched), taken
        from the approved mockup: New coat of paint = mockup 1 (grey stone buttons and
        tabs, white text, darker blocks); New layout = mockup 2 (light Bedrock buttons,
        the settings-pane grey)."""
        st, C = self.style, CRAFTY
        if "crafty" not in st.theme_names():
            st.theme_create("crafty", parent="clam")
        st.theme_use("crafty")
        coat = self.layout == "tabs"
        surface = C["menu"] if coat else C["pane"]          # mockup 1 blocks / mockup 2 pane
        CRAFTY_SURFACE["bg"] = surface
        top = "#1b1b1c"                                      # both mockups' top bars
        body, small, bold, title, row = (crafty_font(k) for k in ("body", "small", "bold", "title", "row"))
        head = ("Silkscreen", -16, "bold") if _CRAFTY_FONTS_LOADED else crafty_font("title")
        st.configure(".", background=surface, foreground=C["ink"], font=body, fieldbackground=C["field"],
                     bordercolor="#000000", lightcolor=surface, darkcolor=surface, troughcolor="#1f1f20",
                     selectbackground=C["green"], selectforeground="#ffffff", insertcolor="#ffffff",
                     focuscolor=C["green_hi"], arrowcolor=C["bar_ink"])
        st.configure("TFrame", background=surface)
        st.configure("Top.TFrame", background=top)
        st.configure("Menu.TFrame", background=C["menu"])
        st.configure("TLabel", background=surface, foreground=C["ink"])
        st.configure("Top.TLabel", background=top, foreground=C["ink"])
        st.configure("Dim.TLabel", background=surface, foreground=C["dim"])
        st.configure("Menu.TLabel", background=C["menu"], foreground=C["ink"])
        # Blocks: black frame with a bevel, and a dark title strip (mockup 1's .block/h3).
        st.configure("TLabelframe", background=surface, bordercolor="#000000", lightcolor="#4a4b4c",
                     darkcolor="#1f1f20", relief="solid", borderwidth=3)
        st.configure("TLabelframe.Label", background="#1f1f20", foreground="#e0e0e0", font=head, padding=(10, 4))

        def bevel_button(name, face, hi, lo, ink, pressed, hover, disabled=("#5f5f5f", "#a8a8a8")):
            st.configure(name, background=face, foreground=ink, bordercolor="#000000", lightcolor=hi, darkcolor=lo,
                         relief="raised", borderwidth=3, focuscolor=C["green_hi"], anchor="center")
            st.map(name, background=[("disabled", disabled[0]), ("pressed", pressed), ("active", hover)],
                   foreground=[("disabled", disabled[1])],
                   lightcolor=[("pressed", lo)], darkcolor=[("pressed", hi)])

        if coat:
            # mockup 1 ".classic": stone grey, white text, light top-left / dark bottom-right
            bevel_button("TButton", "#8b8b8b", "#c6c6c6", "#373737", "#ffffff", "#7a7a7a", "#9a9a9a")
            green = ("#4f9a2f", "#8fd96a", "#24541a", "#ffffff", "#3f7d25", "#5aab37")
        else:
            # mockup 2 ".mc-btn": light Bedrock button, dark text, white top edge, dark bottom
            bevel_button("TButton", C["btn"], "#ffffff", C["btn_lo"], C["bar_ink"], "#b8b9bc", C["btn_hover"],
                         disabled=("#8e8f91", "#55565a"))
            green = (C["green"], C["green_hi"], C["green_lo"], "#ffffff", C["green_lo"], "#4a9a31")
        st.configure("TButton", padding=(12, 5), font=body)
        for name in ("Primary.TButton", "Chosen.TButton"):
            bevel_button(name, *green, disabled=("#5d7a54", "#c9d6c4"))
        st.configure("Primary.TButton", font=bold)
        bevel_button("Danger.TButton", "#b3362c", "#e06a5f", "#6e1c16", "#ffffff", "#8e2a22", "#c4433a")
        st.configure("Success.TButton", foreground=C["green_hi"] if coat else C["green_lo"])
        st.configure("Small.TButton", font=small, padding=(8, 3))
        for name in ("Big.TButton", "Big.Primary.TButton", "Big.Chosen.TButton"):
            st.configure(name, font=row, padding=(18, 9))
        bevel_button("Big.Primary.TButton", *green, disabled=("#5d7a54", "#c9d6c4"))
        bevel_button("Big.Chosen.TButton", *green)
        # Interface row (and other Toolbuttons): the chosen one is green, nothing else.
        face = ("#8b8b8b", "#c6c6c6", "#373737", "#ffffff") if coat else (C["btn"], "#ffffff", C["btn_lo"], C["bar_ink"])
        st.configure("Toolbutton", background=face[0], foreground=face[3], bordercolor="#000000", lightcolor=face[1],
                     darkcolor=face[2], relief="raised", borderwidth=3, padding=(10, 5), font=body)
        st.map("Toolbutton", background=[("selected", green[0]), ("active", "#9a9a9a" if coat else C["btn_hover"])],
               foreground=[("selected", "#ffffff")],
               lightcolor=[("selected", green[1])], darkcolor=[("selected", green[2])])
        # Server strip (mockup ".srv"): dark button; the selected one gets a GREEN OUTLINE,
        # not a green fill, so its running dot stays visible.
        st.configure("Strip.Toolbutton", background=C["menu"], foreground=C["ink"], bordercolor="#000000",
                     lightcolor=C["menu"], darkcolor="#1f1f20", relief="raised", borderwidth=3, padding=(10, 5), font=body)
        st.map("Strip.Toolbutton", background=[("selected", C["pane"]), ("active", "#3c3d3e")],
               bordercolor=[("selected", C["green_hi"])], lightcolor=[("selected", C["green_hi"])],
               darkcolor=[("selected", C["green_hi"])])
        # Tabs (New coat of paint): mockup 1's stone tabs, the selected one green.
        st.configure("TNotebook", background=top, borderwidth=0, tabmargins=(6, 6, 6, 0),
                     bordercolor="#000000", lightcolor=surface, darkcolor=surface)
        st.configure("TNotebook.Tab", background="#8b8b8b", foreground="#ffffff", bordercolor="#000000",
                     lightcolor="#c6c6c6", darkcolor="#373737", padding=(14, 6), font=body)
        st.map("TNotebook.Tab", background=[("selected", "#4f9a2f"), ("active", "#9a9a9a")],
               foreground=[("selected", "#ffffff")], lightcolor=[("selected", "#8fd96a")],
               darkcolor=[("selected", "#24541a")], expand=[("selected", (0, 2, 0, 0))])
        st.configure("Menu.TNotebook", background=surface, borderwidth=0, tabmargins=0,
                     lightcolor=surface, darkcolor=surface)
        st.layout("Menu.TNotebook.Tab", [])   # the left menu chooses the page instead
        # Text fields (mockup ".mc-input"): near-black field, black border, grey inner edge.
        for name in ("TEntry", "TSpinbox"):
            st.configure(name, fieldbackground="#1f1f20", foreground="#ffffff", insertcolor="#ffffff",
                         bordercolor="#000000", lightcolor="#6b6b6b", darkcolor="#6b6b6b", padding=4,
                         background="#8b8b8b" if coat else C["btn"], arrowcolor="#ffffff" if coat else C["bar_ink"])
            st.map(name, lightcolor=[("focus", "#ffffff")], darkcolor=[("focus", "#ffffff")])
        # Dropdowns (mockup ".mc-select"): a light field with a black ▼, black border, dark bottom.
        st.configure("TCombobox", fieldbackground=C["btn"], background=C["btn"], foreground=C["bar_ink"],
                     bordercolor="#000000", lightcolor=C["btn"], darkcolor=C["btn_lo"], arrowcolor="#000000",
                     arrowsize=16, selectbackground=C["btn"], selectforeground=C["bar_ink"], padding=(6, 4))
        # ...with the ▼ drawn inside the field (no separate arrow box), as in Minecraft.
        if not hasattr(self, "_crafty_arrow"):
            img = tk.PhotoImage(width=16, height=10)
            for y in range(8):
                img.put("#000000", to=(y, y + 1, 16 - y, y + 2))
            self._crafty_arrow = img
            try:
                st.element_create("Crafty.downarrow", "image", self._crafty_arrow, sticky="")
            except tk.TclError:
                pass   # already made by an earlier switch to Crafty
        st.layout("TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
            ("Crafty.downarrow", {"side": "right", "sticky": ""}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        st.map("TCombobox", fieldbackground=[("readonly", C["btn"]), ("disabled", "#8e8f91")],
               background=[("readonly", C["btn"]), ("active", C["btn"]), ("disabled", "#8e8f91")],
               foreground=[("disabled", "#55565a")], selectbackground=[("readonly", C["btn"])],
               selectforeground=[("readonly", C["bar_ink"])])
        st.configure("Treeview", background="#262627", fieldbackground="#262627", foreground=C["ink"],
                     rowheight=26, font=body, bordercolor="#000000", lightcolor="#262627", darkcolor="#262627")
        st.map("Treeview", background=[("selected", C["green"])], foreground=[("selected", "#ffffff")])
        st.configure("Treeview.Heading", background=C["bar"], foreground=C["bar_ink"], font=bold, relief="flat",
                     bordercolor="#000000", lightcolor=C["bar"], darkcolor=C["bar"])
        st.configure("TScrollbar", background="#8b8b8b" if coat else C["btn"], troughcolor="#1f1f20",
                     bordercolor="#000000", lightcolor="#c6c6c6" if coat else "#ffffff",
                     darkcolor="#373737" if coat else C["btn_lo"], arrowcolor="#ffffff" if coat else C["bar_ink"])
        st.configure("TProgressbar", background=C["green"], troughcolor="#1f1f20", bordercolor="#000000",
                     lightcolor=C["green_hi"], darkcolor=C["green_lo"])
        for name in ("TCheckbutton", "TRadiobutton"):
            st.configure(name, background=surface, foreground=C["ink"], indicatorbackground="#1f1f20",
                         indicatorforeground=C["green_hi"], font=body)
            st.map(name, background=[("active", surface)], indicatorbackground=[("selected", C["green"])])
        st.configure("TMenubutton", background="#8b8b8b" if coat else C["btn"], foreground="#ffffff" if coat else C["bar_ink"],
                     bordercolor="#000000", lightcolor="#c6c6c6" if coat else "#ffffff",
                     darkcolor="#373737" if coat else C["btn_lo"], arrowcolor="#ffffff" if coat else C["bar_ink"], padding=(10, 5))
        st.configure("TPanedwindow", background=top)
        st.configure("Sash", background=top, sashthickness=6)
        st.configure("TSeparator", background=C["line"])
        # Plain tk widgets (dropdown lists, menus, dialogs) follow via the option database.
        r = self.root
        r.option_add("*TCombobox*Listbox.background", C["btn"])
        r.option_add("*TCombobox*Listbox.foreground", C["bar_ink"])
        r.option_add("*TCombobox*Listbox.selectBackground", C["green"])
        r.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        r.option_add("*TCombobox*Listbox.font", body)
        r.option_add("*Menu.background", C["menu"])
        r.option_add("*Menu.foreground", C["ink"])
        r.option_add("*Menu.activeBackground", C["green"])
        r.option_add("*Menu.activeForeground", "#ffffff")
        r.option_add("*Menu.font", body)
        r.option_add("*Toplevel.background", surface)
        r.configure(bg=top)

    def _paint_tk_widgets(self, top):
        """Crafty colours for the plain tk widgets ttk styles don't reach (console,
        log, canvases, a dialog's own buttons). The Crafty controls paint themselves."""
        if self.look != "crafty":
            return
        C = CRAFTY
        stack = [top]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            cls = w.winfo_class()
            try:
                if cls == "Text" and not getattr(w, "_crafty_painted", False):
                    w.configure(bg=C["console"] if w is getattr(self, "console_text", None) else C["field"],
                                fg=C["ink"], insertbackground="#ffffff", font=crafty_font("console"),
                                selectbackground=C["green"], highlightthickness=0, relief="flat", padx=8, pady=6)
                elif cls in ("TCombobox", "TEntry", "TSpinbox"):
                    w.configure(font=crafty_font("body"))
                elif cls == "Canvas" and not isinstance(w, CraftySlider):
                    if w.master is None or w.master.winfo_class() != "CraftyToggle":
                        w.configure(bg=CRAFTY_SURFACE["bg"], highlightthickness=0)
                elif cls == "Button":
                    if self.layout == "tabs":   # mockup 1 stone button
                        w.configure(bg="#8b8b8b", fg="#ffffff", activebackground="#9a9a9a", relief="raised", bd=3)
                    else:
                        w.configure(bg=C["btn"], fg=C["bar_ink"], activebackground=C["btn_hover"], relief="raised", bd=3)
                elif cls == "Toplevel":
                    w.configure(bg=CRAFTY_SURFACE["bg"])
            except tk.TclError:
                pass

    def _on_toplevel_map(self, event):
        if getattr(self, "look", "classic") == "crafty" and isinstance(event.widget, tk.Toplevel):
            self._paint_tk_widgets(event.widget)

    def _build_interface_row(self, parent, label: bool = True):
        """Settings ▸ Interface: the three looks; the active one is marked (green in Crafty)."""
        row = ttk.Frame(parent)
        if label:
            ttk.Label(row, text="Look and layout:").pack(side=tk.LEFT, padx=(0, 8))
        self._look_var = tk.StringVar(value=self.ui_look)
        for key, text in (("classic", "Classic"), ("coat", "New coat of paint"), ("layout", "New layout")):
            self.choice_button(row, self._look_var, key, text=text,
                               command=lambda k=key: self.set_ui_look(k)).pack(side=tk.LEFT, padx=2)
        return row

    def set_ui_look(self, look: str):
        """Switch look and layout at once -- no restart; Servers are not touched."""
        if look not in UI_LOOKS or look == self.ui_look:
            if hasattr(self, "_look_var"):
                self._look_var.set(self.ui_look)
            return
        if not self._confirm_leave_unsaved():
            self._look_var.set(self.ui_look)
            return
        self.config["ui_look"] = look
        save_config(self.config)
        # Rebuild after this click has finished (its button is about to be destroyed).
        self.root.after_idle(self._rebuild_ui)

    def _current_page_key(self) -> str:
        try:
            current = self.notebook.nametowidget(self.notebook.select())
        except Exception:
            return "server"
        return next((k for k, w in getattr(self, "_pages", {}).items() if w is current), "server")

    def _select_page(self, key: str):
        page = self._pages.get(key) or (self._pages.get("server") if key == "teleport" else None)
        if page is not None:
            self.notebook.select(page)

    def _rebuild_ui(self):
        """Rebuild the window in the chosen look. Everything that matters lives
        outside the widgets (contexts, connections, config, access objects), so a
        rebuild loses nothing: the same Server, page and console come back."""
        keep = {"remote": self.active_remote, "overview": self._overview_kind,
                "page": self._current_page_key(), "geometry": self.root.geometry(),
                "log": self.log_text.get("1.0", "end-1c") if hasattr(self, "log_text") else ""}
        for item, var in self.preserve_vars.items():   # what on_close would save
            if item in self.config.get("preserve_items", {}):
                self.config["preserve_items"][item]["enabled"] = var.get()
        self._sync_flat_settings_into_active_profile()
        self.config["window_geometry"] = keep["geometry"]
        save_config(self.config)
        for kind in list(getattr(self, "_replies", {})):
            self._end_reply_wait(kind)
        self._replies = {}
        self._hide_tooltip()
        for w in list(self.root.winfo_children()):
            w.destroy()   # dialogs too: they belong to the old look
        self.preserve_vars = {}
        self._strip_layout_pending = False
        self._overview_kind = None
        self._init_look()
        if self.look == "classic":
            self.root.option_clear()
            self.style.theme_use(self._classic_base_theme)
        self.setup_styles()
        self.setup_ui()
        self.apply_theme()
        # As load_saved_state, but for the Server that was on screen.
        profile = self.config.get("server_profiles", {}).get(self.config.get("active_profile"), {})
        path = profile.get("path") or self.config.get("last_server_path")
        if path:
            self.server_entry.insert(0, path)
            self.initialize_managers()
        self.validate_inputs()
        self.refresh_sidebar()
        if keep["log"].strip():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert("1.0", keep["log"] + "\n")
            self.log_text.config(state=tk.DISABLED)
        if keep["remote"]:
            self._select_remote_server(*keep["remote"])
        elif keep["overview"]:
            ov = keep["overview"]
            iid = "fleet" if ov[0] == "fleet" else ("machine:local" if ov[1] == "local" else f"rmachine:{ov[2]}")
            try:
                self.sidebar_tree.selection_set(iid)
            except tk.TclError:
                pass
        if not keep["overview"]:
            self._select_page(keep["page"])
        names = {"classic": "Classic", "coat": "New coat of paint", "layout": "New layout"}
        self.log(f"Interface: {names[self.ui_look]}", "info")

    # --- The menu layout ("New layout") --------------------------------------
    MENU_GROUPS = (("Play", ("server", "worlds", "players", "teleport")),
                   ("Set-up", ("config", "backups", "update")),
                   ("General", ("settings",)))
    MENU_TITLES = {"server": "Server", "worlds": "Worlds", "players": "Players", "teleport": "Teleport",
                   "config": "Configuration", "backups": "Backups", "update": "Update", "settings": "Settings"}

    def _build_left_menu(self, parent):
        C = CRAFTY
        menu = tk.Frame(parent, bg=C["menu"], width=250)
        self._menu_items = {}
        self._menu_icons = {}
        for group, keys in self.MENU_GROUPS:
            tk.Label(menu, text=group, bg=C["menu"], fg=C["dim"], font=crafty_font("body"),
                     anchor="w").pack(fill=tk.X, padx=16, pady=(14, 4))
            for key in keys:
                self._menu_icons[key] = pixel_icon(key, 3)
                item = tk.Frame(menu, bg=C["menu"], cursor="hand2", highlightthickness=0)
                bar = tk.Frame(item, bg=C["menu"], width=4)
                bar.pack(side=tk.LEFT, fill=tk.Y)
                ic = tk.Label(item, image=self._menu_icons[key], bg=C["menu"])
                ic.pack(side=tk.LEFT, padx=(10, 10), pady=7)
                tx = tk.Label(item, text=self.MENU_TITLES[key], bg=C["menu"], fg=C["ink"],
                              font=crafty_font("row"), anchor="w")
                tx.pack(side=tk.LEFT, fill=tk.X, expand=True)
                item.pack(fill=tk.X)
                tk.Frame(menu, bg="#252526", height=2).pack(fill=tk.X)
                for w in (item, ic, tx):
                    w.bind("<Button-1>", lambda e, k=key: self._menu_select(k))
                    w.bind("<Enter>", lambda e, k=key: self._menu_hover(k, True))
                    w.bind("<Leave>", lambda e, k=key: self._menu_hover(k, False))
                self._menu_items[key] = (item, bar, ic, tx)
        return menu

    def _menu_paint(self, key: str, state: str):
        C = CRAFTY
        item, bar, ic, tx = self._menu_items[key]
        bg = {"chosen": C["pane"], "hover": "#3c3d3e"}.get(state, C["menu"])
        for w in (item, ic, tx):
            w.configure(bg=bg)
        bar.configure(bg=C["green_hi"] if state == "chosen" else bg)

    def _menu_hover(self, key: str, inside: bool):
        if key != getattr(self, "_menu_current", None):
            self._menu_paint(key, "hover" if inside else "")

    def _menu_select(self, key: str):
        if self._overview_kind:
            self._leave_overview()
        self._select_page(key)

    def _refresh_menu_highlight(self):
        if getattr(self, "_tabbar", None) is not None and getattr(self, "_tabs", None):
            current = self._current_page_key()
            for key, tab in self._tabs.items():
                tab.set_chosen(key == current)
        if self.layout != "menu" or not getattr(self, "_menu_items", None):
            return
        self._menu_current = self._current_page_key()
        for key in self._menu_items:
            self._menu_paint(key, "chosen" if key == self._menu_current else "")

    def open_machines_page(self):
        """New layout: Machines is a page of its own (the Fleet overview)."""
        if self._overview_kind:
            self._leave_overview()
            return
        try:
            self.sidebar_tree.selection_set("fleet")
        except tk.TclError:
            pass

    def _leave_overview(self):
        """Back from the Machines page to the Server that was selected."""
        if self.active_remote:
            iid = f"rprofile:{self.active_remote[0]}:{self.active_remote[1]}"
        else:
            iid = f"profile:{self.config.get('active_profile')}"
        try:
            self.sidebar_tree.selection_set(iid)
        except tk.TclError:
            self._overview_kind = None
            self.show_notebook()

    # --- Teleport page (menu layout) -------------------------------------------
    def setup_teleport_tab(self):
        t = self.teleport_tab
        ttk.Label(t, text="TELEPORT", font=crafty_font("big")).pack(anchor="w")
        ttk.Label(t, text="Move one player to another. Works with cheats off.", style="Dim.TLabel").pack(anchor="w", pady=(4, 10))
        self._tp_info = ttk.Label(t, text="", style="Dim.TLabel")
        self._tp_info.pack(anchor="w")
        self._tp_sel = {"a": None, "b": None}
        self._tp_frames = {}
        for which, title in (("a", "WHO SHOULD MOVE?"), ("b", "TO WHOM?")):
            ttk.Label(t, text=title, font=crafty_font("title")).pack(anchor="w", pady=(16, 6))
            f = ttk.Frame(t)
            f.pack(anchor="w", fill=tk.X)
            self._tp_frames[which] = f
        go_row = ttk.Frame(t)
        go_row.pack(anchor="w", pady=(22, 0), fill=tk.X)
        self._tp_icon = pixel_icon("teleport", 3)
        self._tp_go = self.button(go_row, text="Teleport!", image=self._tp_icon, compound="left",
                                 style="Big.Primary.TButton", command=self._teleport_page_go)
        self._tp_go.pack(side=tk.LEFT)
        self.tp_page_feedback = ttk.Label(go_row, text="", font=crafty_font("row"))
        self.tp_page_feedback.pack(side=tk.LEFT, padx=(16, 0))
        self._reply_slot("teleport", self.tp_page_feedback)
        self.refresh_teleport_page()

    def refresh_teleport_page(self):
        if self.layout != "menu" or not hasattr(self, "_tp_frames"):
            return
        acc = self.active_access
        try:
            online = acc.online_players() if acc else []
        except Exception:
            online = None
        known = list(self.gm_player_combo.cget("values")) if hasattr(self, "gm_player_combo") else []
        everyone = list(online or []) + [n for n in known if n not in (online or [])]
        for name in (self._tp_sel["a"], self._tp_sel["b"]):
            if name and online is not None and name not in online:
                self._tp_sel = {k: (None if v == name else v) for k, v in self._tp_sel.items()}
        for which, frame in self._tp_frames.items():
            for w in frame.winfo_children():
                w.destroy()
            other = self._tp_sel["b" if which == "a" else "a"]
            for i, name in enumerate(everyone):
                is_on = online is None or name in online
                chosen = self._tp_sel[which] == name
                b = self.button(frame, text=name if is_on else f"{name} · offline",
                               style="Big.Chosen.TButton" if chosen else "Big.TButton",
                               command=lambda w=which, n=name: self._teleport_pick(w, n))
                if not is_on or name == other:
                    b.state(["disabled"])
                b.grid(row=i // 4, column=i % 4, padx=(0, 8), pady=(0, 8), sticky="w")
            if not everyone:
                ttk.Label(frame, text="Nobody has joined this Server yet.", style="Dim.TLabel").pack(anchor="w")
        if online is not None and len(online) < 2:
            self._tp_info.config(text=f"Needs two players online — online now: {', '.join(online) if online else 'nobody'}")
        else:
            self._tp_info.config(text="")
        ready = bool(self._tp_sel["a"] and self._tp_sel["b"])
        self._tp_go.state(["!disabled"] if ready else ["disabled"])
        if not self._replies.get("teleport", {}).get("wait") and not self.tp_page_feedback.cget("text"):
            pass

    def _teleport_pick(self, which: str, name: str):
        self._tp_sel[which] = None if self._tp_sel[which] == name else name
        self.refresh_teleport_page()

    def _teleport_page_go(self):
        a, b = self._tp_sel["a"], self._tp_sel["b"]
        if a and b:
            self._do_teleport(a, b)

    # --- Server page + chat-style console (menu layout) -------------------------
    def _setup_server_tab_menu(self):
        C = CRAFTY
        tab = self.server_tab
        home = self._server_home = ttk.Frame(tab)
        home.pack(fill=tk.BOTH, expand=True)
        self._server_title = ttk.Label(home, text="SERVER", font=crafty_font("big"))
        self._server_title.pack(anchor="w")
        self.info_text = ttk.Label(home, text="No Server yet — press + at the top right to create one.", style="Dim.TLabel")
        self.info_text.pack(anchor="w", pady=(4, 10))

        def row(title, desc=""):
            ttk.Separator(home).pack(fill=tk.X)
            r = ttk.Frame(home)
            r.pack(fill=tk.X, pady=8)
            left = ttk.Frame(r)
            left.pack(side=tk.LEFT, fill=tk.X, expand=True)
            ttk.Label(left, text=title, font=crafty_font("row")).pack(anchor="w")
            d = ttk.Label(left, text=desc, style="Dim.TLabel", wraplength=560, justify=tk.LEFT)
            d.pack(anchor="w")
            right = ttk.Frame(r)
            right.pack(side=tk.RIGHT)
            return right, d

        right, _ = row("Status", "Start, stop or restart this Server")
        self.server_running_label = ttk.Label(right, text="⬤ Stopped", foreground=self.fg("red"))
        self.server_running_label.pack(side=tk.LEFT, padx=(0, 14))
        self.start_btn = self.button(right, text="▶ Start", command=self.start_server, style="Primary.TButton")
        self.start_btn.pack(side=tk.LEFT, padx=3)
        self.stop_btn = self.button(right, text="■ Stop", command=self.stop_server)
        self.stop_btn.pack(side=tk.LEFT, padx=3)
        self.restart_btn = self.button(right, text="↻ Restart", command=self.restart_server)
        self.restart_btn.pack(side=tk.LEFT, padx=3)
        right, d = row("Active World", "The World this Server loads")
        self.world_combo = ttk.Combobox(right, state="readonly", width=28)
        self.world_combo.pack(side=tk.LEFT)
        self.world_combo.bind("<<ComboboxSelected>>", self.on_world_selected)
        self.world_hint_label = ttk.Label(d.master, text="", font=self.font(8), foreground=self.fg("#FF9800"))
        self.world_hint_label.pack(anchor="w")
        right, _ = row("Gamerules", "One sleeper skips the night, keep inventory, mob griefing and more")
        self.button(right, text="Open", command=self.open_gamerules_dialog).pack()
        right, d = row("Address for players", "")
        self.network_label = d
        self.network_label.configure(text="Network: Not configured")
        self.button(right, text="Copy", command=self.copy_server_ip).pack()
        right, d = row("Console", "What the Server says, and where you type commands")
        self._console_preview = tk.Label(d.master, text="", bg=C["console"], fg="#dddddd", font=crafty_font("console"),
                                         justify=tk.LEFT, anchor="w", padx=8, pady=4)
        self._console_preview.pack(anchor="w", fill=tk.X, pady=(6, 0))
        self._console_icon = pixel_icon("console", 2)
        self.button(right, text="Open console", image=self._console_icon, compound="left", style="Primary.TButton",
                   command=self.open_console_screen).pack()

        # The console screen: Minecraft's chat, filling the page when opened.
        self._console_follow = tk.BooleanVar(value=True)
        self._console_times = tk.BooleanVar(value=False)
        self._console_filter = tk.BooleanVar(value=False)
        self._console_size = tk.IntVar(value=16)
        self._console_lines = []
        scr = self._console_screen = tk.Frame(tab, bg=C["void"])
        title = tk.Frame(scr, bg=C["bar"])
        title.pack(fill=tk.X)
        back = tk.Label(title, text="‹ Back", bg=C["bar"], fg=C["bar_ink"], font=crafty_font("row"), cursor="hand2", padx=12, pady=6)
        back.pack(side=tk.LEFT)
        back.bind("<Button-1>", lambda e: self.close_console_screen())
        self._console_title = tk.Label(title, text="Console", bg=C["bar"], fg=C["bar_ink"], font=crafty_font("row"))
        self._console_title.pack(side=tk.LEFT, expand=True)
        tk.Frame(scr, bg="#2a2a2a", height=4).pack(fill=tk.X)
        field = tk.Frame(scr, bg=C["console"])
        field.pack(fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(field)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.console_text = tk.Text(field, wrap=tk.WORD, state=tk.DISABLED, bg=C["console"], fg="#ffffff",
                                    insertbackground="#ffffff", relief="flat", highlightthickness=0, padx=12, pady=10,
                                    font=("Pixel Operator Mono", -16) if _CRAFTY_FONTS_LOADED else crafty_font("console"))
        self.console_text._crafty_painted = True
        self.console_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.console_text.tag_config("join", foreground="#ffe066")
        self.console_text.tag_config("me", foreground="#8fd3ff")
        self.console_text.tag_config("ok", foreground="#b6f397")
        sb.config(command=self.console_text.yview)
        self.console_text.config(yscrollcommand=sb.set)
        bar = tk.Frame(scr, bg="#1d1d1e", padx=6, pady=6)
        bar.pack(fill=tk.X)
        self._gear_icon = pixel_icon("settings", 3)
        self.button(bar, image=self._gear_icon, command=self.open_console_options).pack(side=tk.LEFT)
        self.cmd_entry = ttk.Entry(bar, font=crafty_font("row"))
        self.cmd_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, ipady=4)
        self.cmd_entry.bind("<Return>", self.send_server_command)
        self.button(bar, text="Send ➜", style="Big.TButton", command=self.send_server_command).pack(side=tk.LEFT)

    def open_console_screen(self):
        self._server_home.pack_forget()
        self._console_screen.pack(fill=tk.BOTH, expand=True)
        acc = self.active_access
        self._console_title.config(text=f"Console · {self._server_display_name(self.active_remote[0] if self.active_remote else None, self._strip_selected_key())}" if acc else "Console")
        self.console_text.see(tk.END)
        self.cmd_entry.focus_set()

    def close_console_screen(self):
        self._console_screen.pack_forget()
        self._server_home.pack(fill=tk.BOTH, expand=True)

    _CONSOLE_STAMP_RE = re.compile(r"^\[\d{4}-\d\d-\d\d [\d:]+ ?\w*\]\s*")

    def _console_view(self, line: str):
        """How a raw console line is shown in the chat-style console (or None to hide it)."""
        if self._console_filter.get() and not (line.startswith("> ") or PLAYER_JOIN_LEAVE_RE.search(line)):
            return None
        shown = line if self._console_times.get() else self._CONSOLE_STAMP_RE.sub("", line)
        tag = "me" if line.startswith("> ") else "join" if PLAYER_JOIN_LEAVE_RE.search(line) else ""
        return shown, tag

    def _console_append(self, line: str):
        """Menu layout: keep the raw line, show it per the Console options."""
        self._console_lines.append(line)
        cap = self.config.get("console_max_lines", 1000)
        if len(self._console_lines) > cap:
            del self._console_lines[:len(self._console_lines) - cap]
        view = self._console_view(line)
        if view:
            self.console_text.config(state=tk.NORMAL)
            self.console_text.insert(tk.END, view[0] + "\n", view[1])
            lines = int(self.console_text.index("end-1c").split(".")[0])
            if lines > cap:
                self.console_text.delete("1.0", f"{lines - cap}.0")
            if self._console_follow.get():
                self.console_text.see(tk.END)
            self.console_text.config(state=tk.DISABLED)
        self._update_console_preview()

    def _rerender_console(self):
        self.console_text.config(state=tk.NORMAL)
        self.console_text.delete("1.0", tk.END)
        for line in self._console_lines:
            view = self._console_view(line)
            if view:
                self.console_text.insert(tk.END, view[0] + "\n", view[1])
        self.console_text.see(tk.END)
        self.console_text.config(state=tk.DISABLED)
        self._update_console_preview()

    def _update_console_preview(self):
        if hasattr(self, "_console_preview"):
            last = [self._CONSOLE_STAMP_RE.sub("", l) for l in self._console_lines[-3:]]
            self._console_preview.config(text="\n".join(last) if last else "(nothing yet)")

    def open_console_options(self):
        """⚙ in the console: how it's shown (display only), plus the Quick commands."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Console options")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="CONSOLE OPTIONS", font=crafty_font("title")).pack(anchor="w", pady=(0, 10))
        for var, text in ((self._console_follow, "Follow new lines"), (self._console_times, "Show times"),
                          (self._console_filter, "Only players joining and my commands")):
            self.check(body, text=text, variable=var, command=self._rerender_console).pack(anchor="w", pady=4)
        ttk.Separator(body).pack(fill=tk.X, pady=10)
        ttk.Label(body, text="Quick commands").pack(anchor="w")
        q = ttk.Frame(body)
        q.pack(anchor="w", pady=(4, 0))
        for i, cmd in enumerate(["list", "save hold", "save query", "save resume", "say Hello!"]):
            self.button(q, text=cmd, command=lambda c=cmd: self.quick_command(c)).grid(row=i // 3, column=i % 3, padx=3, pady=3, sticky="w")
        ttk.Separator(body).pack(fill=tk.X, pady=10)
        size_row = ttk.Frame(body)
        size_row.pack(fill=tk.X)
        ttk.Label(size_row, text="Text size").pack(side=tk.LEFT)
        size_lbl = ttk.Label(size_row, text=f"{self._console_size.get()} px", style="Dim.TLabel")
        size_lbl.pack(side=tk.RIGHT)

        def resize(v):
            self._console_size.set(v)
            size_lbl.config(text=f"{v} px")
            fam = "Pixel Operator Mono" if _CRAFTY_FONTS_LOADED else "TkFixedFont"
            self.console_text.configure(font=(fam, -int(v)))
        CraftySlider(body, 12, 32, self._console_size.get(), command=resize, width=380).pack(fill=tk.X, pady=(4, 0))
        self.button(body, text="Close", command=dlg.destroy).pack(anchor="e", pady=(14, 0))
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        self._paint_tk_widgets(dlg)

    # --- New layout: one row per choice on every page (mockup 2) -------------------
    # The rows reuse the tab layout's handlers unchanged. Lists (Worlds, players,
    # backups) are kept in the same Treeviews as in the other looks, just never
    # shown: a row's buttons select its item there and call the same method.

    def _menu_scroll_page(self, tab):
        """A scrolling column for a New layout page; returns the frame to fill."""
        canvas = tk.Canvas(tab, highlightthickness=0, bd=0, bg=CRAFTY_SURFACE["bg"])
        sb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=(0, 0, 14, 14))
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        wheel_seqs = ("<MouseWheel>", "<Button-4>", "<Button-5>")

        def wheel(e):
            if canvas.yview() == (0.0, 1.0):
                return
            step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
            canvas.yview_scroll(step * 3, "units")

        def enter(e):
            for seq in wheel_seqs:
                canvas.bind_all(seq, wheel)

        def leave(e):
            # Moving onto a row is a <Leave> too; only let go once the pointer is off the page.
            under = canvas.winfo_containing(*canvas.winfo_pointerxy())
            if under is None or not str(under).startswith(str(canvas)):
                for seq in wheel_seqs:
                    canvas.unbind_all(seq)
        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)
        return inner

    def _menu_head(self, parent, title: str, desc: str = ""):
        ttk.Label(parent, text=title, font=crafty_font("big")).pack(anchor="w")
        d = ttk.Label(parent, text=desc, style="Dim.TLabel", wraplength=760, justify=tk.LEFT)
        d.pack(anchor="w", pady=(4, 10))
        return d

    def _menu_section(self, parent, title: str, small: str = ""):
        f = ttk.Frame(parent)
        f.pack(fill=tk.X, pady=(18, 4))
        ttk.Label(f, text=title.upper(), font=crafty_font("title")).pack(anchor="w")
        if small:
            ttk.Label(f, text=small, style="Dim.TLabel", wraplength=760, justify=tk.LEFT).pack(anchor="w")
        return f

    def _menu_item(self, parent, title: str, desc: str = ""):
        """One row: title and a grey explanation on the left, the control on the right.
        Returns (right, desc label, row box); box.left / box.title_row take extras."""
        box = ttk.Frame(parent)
        box.pack(fill=tk.X)
        ttk.Separator(box).pack(fill=tk.X)
        r = ttk.Frame(box)
        r.pack(fill=tk.X, pady=8)
        right = ttk.Frame(r)
        right.pack(side=tk.RIGHT)
        left = ttk.Frame(r)
        left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title_row = ttk.Frame(left)
        title_row.pack(anchor="w")
        ttk.Label(title_row, text=title, font=crafty_font("row")).pack(side=tk.LEFT)
        d = ttk.Label(left, text=desc, style="Dim.TLabel", wraplength=520, justify=tk.LEFT)
        d.pack(anchor="w", after=title_row)
        self._hide_when_empty(d, anchor="w", after=title_row)
        box.left, box.title_row = left, title_row
        return right, d, box

    @staticmethod
    def _hide_when_empty(label, **pack):
        """Keep a label out of the layout while its text is empty (a row without an
        explanation, an answer line that hasn't been given yet), back once it has text."""
        original = label.configure

        def configure(cnf=None, **kw):
            result = original(cnf, **kw)
            if "text" in kw or (cnf and "text" in cnf):
                if not str(label.cget("text")):
                    label.pack_forget()
                elif not label.winfo_manager():
                    label.pack(**pack)
            return result
        label.configure = label.config = configure
        if not str(label.cget("text")):
            label.pack_forget()

    def _menu_tag(self, box, text: str, on: bool = True):
        """A small marker next to a row's title (ACTIVE, ONLINE, offline)."""
        ttk.Label(box.title_row, text=text, font=crafty_font("bold" if on else "small"),
                  foreground=CRAFTY["green_hi"] if on else CRAFTY["dim"]).pack(side=tk.LEFT, padx=(10, 0))

    def _menu_slider(self, parent, var, lo: int, hi: int, unit: str = "", width: int = 260):
        """A green slider bound to an IntVar both ways (a profile switch sets the var)."""
        v = int(var.get())
        lbl = ttk.Label(parent, text=f"{v}{unit}", width=6, anchor="e")

        def moved(nv):
            lbl.config(text=f"{nv}{unit}")
            if int(var.get()) != int(nv):
                var.set(int(nv))
        slider = CraftySlider(parent, min(lo, v), max(hi, v), v, width=width, command=moved)
        slider.pack(side=tk.LEFT)
        lbl.pack(side=tk.LEFT, padx=(8, 0))

        def follow(*_):
            try:
                if str(var.get()) != slider.get():
                    slider.set(int(var.get()))
            except (tk.TclError, ValueError):
                pass
        trace = var.trace_add("write", follow)
        slider.bind("<Destroy>", lambda e: var.trace_remove("write", trace) if e.widget is slider else None, add="+")
        return slider

    def _tree_do(self, tree, iid, action, *args):
        """A row's button: select its item in the (hidden) list, then run the same
        handler the list's buttons run in the other looks."""
        if tree.exists(iid):
            tree.selection_set(iid)
            tree.focus(iid)
            action(*args)

    def _menu_list(self, kind: str, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X)
        self.__dict__.setdefault("_menu_lists", {})[kind] = frame
        return frame

    def _sync_menu_rows(self, kind: str):
        """Rebuild a New layout list from its Treeview (after every refresh)."""
        frame = self.__dict__.get("_menu_lists", {}).get(kind)
        if self.layout != "menu" or frame is None or not frame.winfo_exists():
            return
        for w in frame.winfo_children():
            w.destroy()
        getattr(self, f"_menu_rows_{kind}")(frame)
        self._paint_tk_widgets(frame)

    # Worlds
    def _setup_worlds_tab_menu(self):
        p = self._menu_scroll_page(self.worlds_tab)
        hidden = ttk.Frame(self.worlds_tab)   # never shown
        self.world_tree = ttk.Treeview(hidden, columns=("name", "size", "last_modified", "version"), show="tree headings")
        self.world_tree.bind("<<TreeviewSelect>>", self.on_world_select)
        self.world_info_label = ttk.Label(hidden, text="")
        self._menu_head(p, "WORLDS", "The saves this Server can load. Exactly one is active. A World won't load on a "
                                     "Bedrock Server Version older than the one it last ran on.")
        right, _, _ = self._menu_item(p, "Create a new World", "A name and an optional seed. Bedrock generates it on the next start.")
        self.button(right, text="✨ Create", style="Primary.TButton", command=self.create_new_world).pack()
        right, _, _ = self._menu_item(p, "Worlds folder", "Open it on this computer")
        self.button(right, text="Open folder", command=self.open_worlds_folder).pack()
        self._menu_section(p, "This Server's Worlds")
        self._menu_list("worlds", p)
        self._sync_menu_rows("worlds")

    def _menu_rows_worlds(self, frame):
        tree = self.world_tree
        if not tree.get_children():
            ttk.Label(frame, text="No Worlds yet.", style="Dim.TLabel").pack(anchor="w", pady=8)
            return
        for iid in tree.get_children():
            name, size, changed, version = (str(v) for v in tree.item(iid, "values"))
            tags = tree.item(iid, "tags")
            active, pending = "active" in tags, "pending" in tags
            desc = ("Made on the next start: set it up in Configuration, then Start" if pending
                    else f"Last run on {version} · {size} · changed {changed}")
            right, d, box = self._menu_item(frame, name, desc)
            if pending:
                d.configure(foreground=CRAFTY["warn"])
            if active:
                self._menu_tag(box, "ACTIVE")
            b = self.button(right, text="Set active", command=lambda i=iid: self._tree_do(tree, i, self.set_selected_world_active))
            b.pack(side=tk.LEFT, padx=2)
            if active:
                b.state(["disabled"])
            self.button(right, text="Rename", command=lambda i=iid: self._tree_do(tree, i, self.rename_selected_world)).pack(side=tk.LEFT, padx=2)
            b = self.button(right, text="Delete", command=lambda i=iid: self._tree_do(tree, i, self.delete_selected_world))
            b.pack(side=tk.LEFT, padx=2)
            if active:
                b.state(["disabled"])

    # Players
    def _setup_players_tab_menu(self):
        p = self._menu_scroll_page(self.players_tab)
        hidden = ttk.Frame(self.players_tab)
        self.allow_tree = ttk.Treeview(hidden, columns=("name", "xuid", "ignores_limit"), show="headings")
        self.perm_tree = ttk.Treeview(hidden, columns=("name", "xuid", "permission"), show="headings")
        self.perm_player_combo = ttk.Combobox(hidden)
        self.gm_player_combo = ttk.Combobox(hidden)
        self._menu_head(p, "PLAYERS", "Who may join, their role, and their game mode")
        self.allowlist_var = tk.BooleanVar(value=False)
        right, _, _ = self._menu_item(p, "Only players on the list may join",
                                      "Bedrock has no blacklist: leaving someone off the list keeps them out")
        self.check(right, variable=self.allowlist_var, command=self.toggle_allowlist_enforcement).pack()
        right, _, _ = self._menu_item(p, "Add a player to the list", "By their exact Xbox gamertag")
        self.button(right, text="Add", command=self.add_allowlist_player).pack()
        sec = self._menu_section(p, "Each player", "A role, and a game mode that changes right away for a player who is online")
        self.gm_feedback = ttk.Label(sec, text="", font=crafty_font("row"))
        self._hide_when_empty(self.gm_feedback, anchor="w", pady=(4, 0))
        self.gm_force_warn = ttk.Label(sec, text="", foreground=CRAFTY["bad"], wraplength=760, justify=tk.LEFT)
        self._hide_when_empty(self.gm_force_warn, anchor="w")
        self._reply_slot("gamemode", self.gm_feedback)
        self._menu_list("players", p)
        right, _, _ = self._menu_item(p, "Someone missing?", "Names and XUIDs are learned when players join; this re-reads the whole console")
        self.button(right, text="Scan console", command=self.scan_console_for_players).pack()
        self._sync_menu_rows("players")

    def _menu_rows_players(self, frame):
        names = list(self.gm_player_combo.cget("values"))
        if not names:
            ttk.Label(frame, text="Nobody has joined this Server yet. Names appear here when players join.",
                      style="Dim.TLabel").pack(anchor="w", pady=8)
            return
        allow = {str(self.allow_tree.item(i, "values")[0]).lower(): i for i in self.allow_tree.get_children()}
        perms = {str(self.perm_tree.item(i, "values")[0]).lower(): i for i in self.perm_tree.get_children()}
        acc = self.active_access
        try:
            online = acc.online_players() if acc else None
        except Exception:
            online = None
        online_low = {n.lower() for n in online} if online is not None else None
        for name in names:
            low = name.lower()
            role = str(self.perm_tree.item(perms[low], "values")[2]) if low in perms else ""
            on_list = low in allow
            desc = f"Role: {role or 'the default for new players'} · {'on the list' if on_list else 'not on the list'}"
            right, _, box = self._menu_item(frame, name, desc)
            if online_low is not None:
                self._menu_tag(box, "ONLINE" if low in online_low else "offline", low in online_low)
            role_box = ttk.Combobox(right, state="readonly", width=9, values=["Default", "Visitor", "Member", "Operator"])
            role_box.set(role.capitalize() if role else "Default")
            role_box.bind("<<ComboboxSelected>>",
                          lambda e, n=name, b=role_box: self.root.after_idle(lambda: self._menu_set_role(n, b.get())))
            role_box.pack(side=tk.LEFT, padx=(0, 10))
            self.create_tooltip(role_box, f"{name}'s role on this Server")
            for mode in ("Survival", "Creative", "Adventure"):
                self.button(right, text=mode, command=lambda n=name, m=mode.lower(): self._menu_set_gamemode(n, m)
                            ).pack(side=tk.LEFT, padx=2)
            if on_list:
                self.button(right, text="Off the list",
                            command=lambda i=allow[low]: self._tree_do(self.allow_tree, i, self.remove_allowlist_player)
                            ).pack(side=tk.LEFT, padx=(10, 0))

    def _menu_set_role(self, name: str, role: str):
        if role == "Default":
            iid = next((i for i in self.perm_tree.get_children()
                        if str(self.perm_tree.item(i, "values")[0]).lower() == name.lower()), None)
            if iid:
                self._tree_do(self.perm_tree, iid, self.remove_permission_entry)
            return
        self.perm_player_combo.set(name)
        self.set_player_permission(role.lower())

    def _menu_set_gamemode(self, name: str, mode: str):
        self.gm_player_combo.set(name)
        self.set_player_gamemode(mode)

    # Backups
    def _setup_backup_tab_menu(self):
        p = self._menu_scroll_page(self.backup_tab)
        hidden = ttk.Frame(self.backup_tab)
        self.backup_tree = ttk.Treeview(hidden, columns=("name", "date", "size"), show="headings")
        self.backup_header_label = self._menu_head(p, "BACKUPS", "Backups for: (no Server selected)")
        right, _, _ = self._menu_item(p, "Make a backup now", "Copies what's switched on below")
        self.button(right, text="Create backup", style="Primary.TButton", command=self.manual_backup).pack()
        self._menu_section(p, "What to back up", "Also what an update keeps")
        self.preserve_inner = ttk.Frame(p)
        self.preserve_inner.pack(fill=tk.X)
        for item, props in self.config["preserve_items"].items():
            var = tk.BooleanVar(value=props["enabled"])
            self.preserve_vars[item] = var
            right, _, _ = self._menu_item(self.preserve_inner, f"⭐ {item}" if props.get("critical") else item,
                                          props.get("description", ""))
            self.check(right, variable=var).pack()
        self._menu_section(p, "Backup settings", "For this Server")
        self.max_backups_var = tk.IntVar(value=self.config.get("max_backups", 5))
        right, _, _ = self._menu_item(p, "Backups to keep", "The oldest ones are removed when there are more")
        self._menu_slider(right, self.max_backups_var, 1, 50)
        self.auto_cleanup_var = tk.BooleanVar(value=self.config.get("auto_cleanup_backups", True))
        right, _, _ = self._menu_item(p, "Clean up after an update", "Removes the oldest backups once an update is done")
        self.check(right, variable=self.auto_cleanup_var).pack()
        self.compress_var = tk.BooleanVar(value=self.config.get("compress_backups", False))
        right, _, _ = self._menu_item(p, "Compress backups", "ZIP files: smaller, but slower to make")
        self.check(right, variable=self.compress_var).pack()
        right, _, _ = self._menu_item(p, "Save these settings", "")
        self.button(right, text="Save", style="Primary.TButton", command=self.save_settings).pack()
        self._menu_section(p, "Backups")
        self._menu_list("backups", p)
        right, _, _ = self._menu_item(p, "Refresh the list", "Sizing every backup can take a moment")
        self.button(right, text="Refresh", command=self.refresh_backups).pack()
        right, _, _ = self._menu_item(p, "Clean up old backups", "Keeps only the newest ones, as many as set above")
        self.button(right, text="Clean up", command=self.cleanup_backups).pack()
        right, _, _ = self._menu_item(p, "Backups folder", "Open it on this computer")
        self.button(right, text="Open folder", command=self.open_backup_folder).pack()
        self._sync_menu_rows("backups")

    def _menu_rows_backups(self, frame):
        tree = self.backup_tree
        if not tree.get_children():
            ttk.Label(frame, text="No backups yet.", style="Dim.TLabel").pack(anchor="w", pady=8)
            return
        for iid in tree.get_children():
            name, date, size = (str(v) for v in tree.item(iid, "values"))
            right, _, _ = self._menu_item(frame, name, f"{date} · {size}")
            for text, fn in (("Restore", self.restore_selected_backup), ("Delete", self.delete_selected_backup),
                             ("Open", self.open_selected_backup)):
                self.button(right, text=text, command=lambda i=iid, f=fn: self._tree_do(tree, i, f)).pack(side=tk.LEFT, padx=2)

    # Update
    def _setup_main_tab_menu(self):
        p = self._menu_scroll_page(self.main_tab)
        self._menu_head(p, "UPDATE", "The Bedrock Server Version: the game engine itself. Your Worlds survive an update.")
        right, d, box = self._menu_item(p, "This Server", "Installed: —   ·   Latest: —")
        self._update_this_row = box
        self.update_version_label = d
        self.update_state_label = ttk.Label(box.left, text="", style="Dim.TLabel", wraplength=520, justify=tk.LEFT)
        self.update_state_label.pack(anchor="w")
        self._hide_when_empty(self.update_state_label, anchor="w")
        self.update_button = self.button(right, text="Update", command=self.start_update, style="Primary.TButton")
        self.update_button.pack()
        right, _, self._update_all_row = self._menu_item(p, "All Servers on this computer",
                                                         "One after the other, each backed up first")
        self.update_all_button = self.button(right, text="Update all Servers", command=self.update_all_servers)
        self.update_all_button.pack()
        right, _, _ = self._menu_item(p, "Check for a new version", "Asks Mojang for the newest release")
        self.button(right, text="Check again", command=lambda: self.refresh_update_status(force=True)).pack()
        self._menu_section(p, "When updating", "For this Server")
        self.auto_stop_var = tk.BooleanVar(value=self.config.get("auto_stop_server_before_update", True))
        right, _, _ = self._menu_item(p, "Stop the Server first", "It's stopped nicely, then updated")
        self.check(right, variable=self.auto_stop_var).pack()
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start_server_after_update", False))
        right, _, _ = self._menu_item(p, "Start it again afterwards", "")
        self.check(right, variable=self.auto_start_var).pack()
        right, _, _ = self._menu_item(p, "Save these settings", "")
        self.button(right, text="Save", style="Primary.TButton", command=self.save_settings).pack()
        right, _, box = self._menu_item(p, "Advanced", "A specific ZIP, or type a version (if Mojang can't be reached)")
        self.advanced_toggle = self.button(right, text="Advanced ▸", command=self._toggle_update_advanced)
        self.advanced_toggle.pack()
        holder = ttk.Frame(box)
        holder.pack(fill=tk.X)
        holder.columnconfigure(0, weight=1)
        self._build_update_advanced(holder)
        self._menu_section(p, "Progress")
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(p, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(4, 2))
        self.progress_label = ttk.Label(p, text="Ready")
        self.progress_label.pack(anchor="w")
        right, _, _ = self._menu_item(p, "Dry run", "Shows what an update would do, without changing anything")
        self.button(right, text="Dry run", command=self.dry_run).pack()
        right, _, _ = self._menu_item(p, "Server folder", "Open it on this computer")
        self.button(right, text="Open folder", command=self.open_server_folder).pack()
        self._menu_section(p, "Activity log")
        log_frame = ttk.Frame(p, height=220)
        log_frame.pack(fill=tk.X)
        log_frame.pack_propagate(False)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self._build_activity_log(log_frame)

    # Settings
    def _setup_settings_tab_menu(self):
        p = self._menu_scroll_page(self.settings_tab)
        self._menu_head(p, "SETTINGS", "For the whole app, on this computer")
        self._menu_section(p, "Interface")
        right, _, _ = self._menu_item(p, "Look and layout", "Classic is always here if one of the new looks misbehaves")
        self._build_interface_row(right, label=False).pack()
        self.font_size_var = tk.IntVar(value=self.config.get("console_font_size", 9))
        right, _, _ = self._menu_item(p, "Console text size in the other looks",
                                      "Classic and New coat of paint. The chat console here has its own size under ⚙.")
        ttk.Spinbox(right, from_=6, to=24, width=6, textvariable=self.font_size_var).pack()
        self.notifications_var = tk.BooleanVar(value=self.config.get("show_notifications", True))
        right, _, _ = self._menu_item(p, "Show notification messages", "")
        self.check(right, variable=self.notifications_var).pack()
        self.dark_mode_var = tk.BooleanVar(value=self.config.get("dark_mode", False))
        right, _, _ = self._menu_item(p, "🌙 Dark mode", "The new looks are always dark; Classic can be light or dark")
        self.check(right, variable=self.dark_mode_var, command=self.toggle_dark_mode, state="disabled").pack()
        self.check_updates_var = tk.BooleanVar(value=self.config.get("check_updates_on_start", True))
        right, _, _ = self._menu_item(p, "Look for a new Bedrock Server Version at start", "")
        self.check(right, variable=self.check_updates_var).pack()

        ra_cfg = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        self._menu_section(p, "Remote administration", "Let another computer on your home network run these Servers")
        self.remote_enabled_var = tk.BooleanVar(value=bool(ra_cfg.get("enabled")))
        right, _, box = self._menu_item(p, "Allow it", "Home network only: don't port-forward this. On the other computer, "
                                                       "➕ Machine with this computer's address and the token.")
        self.check(right, variable=self.remote_enabled_var, command=self.toggle_remote_admin).pack()
        self.remote_status_label = ttk.Label(box.left, text="", style="Dim.TLabel")
        self.remote_status_label.pack(anchor="w")
        self._hide_when_empty(self.remote_status_label, anchor="w")
        self.remote_port_var = tk.IntVar(value=ra_cfg.get("port", REMOTE_DEFAULT_PORT))
        right, _, _ = self._menu_item(p, "Port", "")
        ttk.Spinbox(right, from_=1024, to=65535, width=8, textvariable=self.remote_port_var).pack()
        self.remote_token_var = tk.StringVar(value=ra_cfg.get("token", "") or "(generated when enabled)")
        right, _, _ = self._menu_item(p, "Pairing token", "The other computer needs it once, to pair")
        ttk.Entry(right, textvariable=self.remote_token_var, state="readonly", width=20).pack(side=tk.LEFT, padx=(0, 6))
        self.button(right, text="Copy", command=self.copy_remote_token).pack(side=tk.LEFT, padx=2)
        self.button(right, text="New token", command=self.regenerate_remote_token).pack(side=tk.LEFT, padx=2)

        self._menu_section(p, "This Server's folder")
        right, _, box = self._menu_item(p, "Folder", "Moves the selected Server to another folder. To add a Server, use + at the top right.")
        self.server_entry = ttk.Entry(box.left)
        self.server_entry.pack(fill=tk.X, pady=(6, 0))
        self.server_entry.bind("<KeyRelease>", self.validate_inputs)
        self.server_status = ttk.Label(box.left, text="", foreground=self.fg("gray"))
        self.server_status.pack(anchor="w")
        self._hide_when_empty(self.server_status, anchor="w")
        self.button(right, text="Browse", command=self.browse_server).pack()

        self._menu_section(p, "Saving")
        right, _, _ = self._menu_item(p, "Save settings", "The settings on this page are kept when you press Save")
        self.button(right, text="Save", style="Primary.TButton", command=self.save_settings).pack()
        right, _, _ = self._menu_item(p, "Reset to defaults", "Puts the settings on this page back")
        self.button(right, text="Reset", command=self.reset_settings).pack()
        right, _, _ = self._menu_item(p, "Settings file", "Open it in a text editor")
        self.button(right, text="Open", command=self.open_config_file).pack()

        self._menu_section(p, "About")
        self._menu_item(p, f"{APP_NAME} {APP_VERSION_LABEL}",
                        f"{APP_AUTHOR}. A tool for managing Minecraft Bedrock Dedicated Servers.")

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
        # Delegates to the module-level function (shared with ServerService /
        # the headless --agent); kept as a method so existing call sites and
        # ServerPropertiesEditor (self.app.parse_server_properties) are unchanged.
        return parse_server_properties(filepath)

    def save_server_properties(self, filepath: Path, props: Dict[str, str]):
        ok = save_server_properties(filepath, props)
        if not ok:
            self.log("Error saving properties", "error")
        return ok

    def setup_keyboard_shortcuts(self):
        self.root.bind("<Control-o>", lambda e: self.browse_server())
        self.root.bind("<Control-s>", lambda e: self.manual_backup())
        self.root.bind("<Control-u>", lambda e: self.start_update())
        self.root.bind("<F5>", lambda e: self.validate_inputs())
        self.root.bind("<F1>", lambda e: self.show_about())
    
    def setup_ui(self):
        self.root.title(f"{APP_NAME} {APP_VERSION_LABEL}")
        self.root.geometry(self.config.get("window_geometry", "1200x700"))
        self.root.minsize(800, 600)
        self.set_window_icon()

        # A slim top bar holding the sidebar's show/hide toggle -- kept OUTSIDE
        # the PanedWindow so it's always reachable even while the sidebar
        # itself is collapsed (the only way back once it's hidden).
        top_bar = ttk.Frame(self.root, style="Top.TFrame")
        top_bar.pack(fill=tk.X, side=tk.TOP, padx=5, pady=(5, 0))
        if self.layout == "menu":   # New layout: Machines is a page of its own
            self._machines_icon = pixel_icon("machines", 2)
            self.sidebar_toggle_btn = self.button(top_bar, image=self._machines_icon, compound="left",
                                                 command=self.open_machines_page)
        else:
            self.sidebar_toggle_btn = self.button(top_bar, command=self.toggle_sidebar)
        self.sidebar_toggle_btn.pack(side=tk.LEFT)
        # Server strip (3.0, docs/V3-SERVERS-PLAN.md §1): one button per Server
        # of the selected Machine, "+"/"−" pinned right. "Server N" is display-
        # only: N is the position in the list, never stored anywhere.
        self.server_remove_btn = self.button(top_bar, text="−", width=3, command=self.remove_server_dialog)
        self.server_remove_btn.pack(side=tk.RIGHT)
        self.server_add_btn = self.button(top_bar, text="+", width=3, command=self.new_server_dialog)
        self.server_add_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self.server_rename_btn = self.button(top_bar, text="✏", width=3, command=self.rename_server_dialog)
        self.server_rename_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self.create_tooltip(self.server_rename_btn, "Rename the selected Server")
        self.server_strip = ttk.Frame(top_bar, style="Top.TFrame")
        self.server_strip.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.server_strip.bind("<Configure>", self._on_strip_resize)
        self._strip_choice = tk.StringVar(value="")
        self._strip_layout_pending = False
        self._strip_last_width = 0

        # Sidebar (Machines -> Servers) + the existing tabbed Notebook, side by
        # side in a resizable pane. See docs/V2-MAJORDOMO-PLAN.md, "GUI: sidebar
        # + the same 7 tabs" -- Stage 1 only ever has "This computer" as a
        # Machine; remote Machines arrive in Stage 3. Collapsible (default
        # collapsed) so single-Server users get the plain 1.0.4-style look;
        # the toggle button above brings it back for Fleet/multi-Server use.
        self.main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.sidebar_frame = ttk.Frame(self.main_pane, width=200)
        self.setup_sidebar(self.sidebar_frame)
        self._sidebar_collapsed = bool(self.config.get("sidebar_collapsed", True))
        if self.layout == "menu":
            # The sidebar tree still exists (its selection drives Fleet/Servers), unseen;
            # the left menu takes its place.
            self.left_menu = self._build_left_menu(self.main_pane)
            self.main_pane.add(self.left_menu, weight=0)
        elif not self._sidebar_collapsed:
            self.main_pane.add(self.sidebar_frame, weight=0)
        self._update_sidebar_toggle_btn()

        notebook_frame = ttk.Frame(self.main_pane)
        self.main_pane.add(notebook_frame, weight=1)

        self._tabbar = None
        if self.look == "crafty" and self.layout == "tabs":
            # New coat of paint: the tab row is drawn (shadowed text, as in mockup 1);
            # the notebook below keeps its pages, without its own tab row.
            self._tabbar = ttk.Frame(notebook_frame, style="Top.TFrame", padding=(6, 6, 6, 4))
            self._tabbar.pack(fill=tk.X)
        if self.layout == "menu" or self._tabbar is not None:
            self.notebook = ttk.Notebook(notebook_frame, style="Menu.TNotebook")
        else:
            self.notebook = ttk.Notebook(notebook_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # Overview pane (Fleet / Machine page) shown INSTEAD of the notebook
        # when the sidebar's Fleet root or a Machine node is selected -- see
        # docs/V2-MAJORDOMO-PLAN.md, "Fleet overview + machine page". Built
        # fresh each time it's shown; toggled via show_notebook()/show_overview().
        self.overview_frame = ttk.Frame(notebook_frame, padding=15)
        self._overview_kind = None

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

        self._pages = {"server": self.server_tab, "worlds": self.worlds_tab, "players": self.players_tab,
                       "config": self.properties_editor, "backups": self.backup_tab, "update": self.main_tab,
                       "settings": self.settings_tab}
        if self._tabbar is not None:
            self._tabs = {}
            for key in ("server", "worlds", "players", "config", "backups", "update", "settings"):
                t = CraftyTab(self._tabbar, self.MENU_TITLES[key], lambda k=key: self._menu_select(k), crafty_font("body"))
                t.pack(side=tk.LEFT, padx=(0, 4))
                self._tabs[key] = t
        if self.layout == "menu":   # Teleport is a menu item of its own in the New layout
            self.teleport_tab = ttk.Frame(self.notebook, padding=20)
            self.notebook.add(self.teleport_tab, text="📍 Teleport")
            self._pages["teleport"] = self.teleport_tab
            self.setup_teleport_tab()

        # Auto-refresh dynamic tabs when they're opened, so lists never go stale
        # (bound after construction, so startup tab-selection can't fire it early).
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)
        
        self.status_bar = ttk.Frame(self.root, style="Top.TFrame")
        self.status_bar.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        self.status_label = ttk.Label(self.status_bar, text="Ready", style="Top.TLabel")
        self.status_label.pack(side=tk.LEFT)
        self.server_status_label = ttk.Label(self.status_bar, text="⬤ Server: Not configured", foreground=self.fg("gray"),
                                             style="Top.TLabel")
        self.server_status_label.pack(side=tk.RIGHT)
        if self.layout == "menu":   # keep the status line visible below long pages
            self.status_bar.pack_configure(before=self.main_pane)
        if self.look == "crafty":
            self._paint_tk_widgets(self.root)
        self._refresh_menu_highlight()

    def setup_sidebar(self, parent):
        # Heading over the whole tree, which lists ALL Machines (this computer +
        # any paired remote ones). The "This computer" *node* inside the tree is
        # a separate thing -- one Machine among possibly several.
        ttk.Label(parent, text="🖥 Machines", font=self.font(9, "bold")).pack(
            anchor="w", padx=6, pady=(6, 2))
        self.sidebar_tree = ttk.Treeview(parent, show="tree", height=15)
        self.sidebar_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.sidebar_tree.bind("<<TreeviewSelect>>", self.on_sidebar_select)
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, padx=4, pady=(0, 6))
        # Adding a Server is the top line's "+" since 3.0.
        self.button(btns, text="➕ Machine", command=self.add_machine_dialog).pack(fill=tk.X)

    def toggle_sidebar(self):
        """Show/hide the Machines/Servers sidebar. Collapsed = plain 1.0.4-style
        single-Server look; expanded = Fleet/multi-Server/multi-Machine view.
        Persisted so the choice survives a restart."""
        self._sidebar_collapsed = not self._sidebar_collapsed
        if self._sidebar_collapsed:
            self.main_pane.forget(self.sidebar_frame)
        else:
            self.main_pane.insert(0, self.sidebar_frame, weight=0)
        self._update_sidebar_toggle_btn()
        self.config["sidebar_collapsed"] = self._sidebar_collapsed
        save_config(self.config)

    def _update_sidebar_toggle_btn(self):
        if self.layout == "menu":
            self.sidebar_toggle_btn.config(text="Machines")
            return
        if self._sidebar_collapsed:
            self.sidebar_toggle_btn.config(text="▶ Machines")
        else:
            self.sidebar_toggle_btn.config(text="◀ Hide")

    # --- Top-line Server strip (3.0) -------------------------------------
    STRIP_WORLD_MAX = 22  # characters of the World name before "…"

    def _machine_server_entries(self, machine_id: Optional[str] = None) -> List[dict]:
        """One Machine's Servers in list order -- this computer's when machine_id
        is None. Each: key, number, world (Active World, full), running, online
        (count, or None when unknown). The top line, the sidebar, the Fleet
        overview and the Machine pages all label Servers from this, so a Server
        is "Server N: <Active World>" everywhere."""
        entries = []
        if machine_id:
            servers = self._remote_state.get(machine_id, {}).get("servers", [])
            for n, sv in enumerate(servers, 1):
                # Hosts before 3.1 don't report "world"; their list name is the best we have.
                entries.append({"key": f"r|{machine_id}|{sv['id']}", "number": n,
                                "world": sv.get("world") or sv.get("name") or "Server",
                                "running": bool(sv.get("running")), "online": None})
            return entries
        for n, (pid, profile) in enumerate(self.config.get("server_profiles", {}).items(), 1):
            ctx = self.contexts.get(pid)
            entries.append({"key": f"l|{pid}", "number": n,
                            "world": _active_world_of(profile) or profile.get("name") or "Server",
                            "running": bool(ctx and ctx.is_running()),
                            "online": len(ctx.online_players()) if ctx else 0})
        return entries

    def _strip_entries(self) -> List[dict]:
        """The Servers of the Machine currently in view (see _machine_server_entries)."""
        remote_mid = None
        if self.active_remote:
            remote_mid = self.active_remote[0]
        elif self._overview_kind and self._overview_kind[0] == "machine" and self._overview_kind[1] == "remote":
            remote_mid = self._overview_kind[2]
        return self._machine_server_entries(remote_mid)

    def _strip_selected_key(self) -> str:
        if self._overview_kind:
            return ""  # Fleet / Machine page: no single Server is selected
        if self.active_remote:
            return f"r|{self.active_remote[0]}|{self.active_remote[1]}"
        pid = self.config.get("active_profile")
        return f"l|{pid}" if pid else ""

    @classmethod
    def _strip_label(cls, e: dict, short: bool = True) -> str:
        world = e["world"]
        if short and len(world) > cls.STRIP_WORLD_MAX:
            world = world[:cls.STRIP_WORLD_MAX - 1].rstrip() + "…"
        return f"{'🟢' if e['running'] else '⚪'} Server {e['number']}: {world}"

    def _on_strip_resize(self, event):
        # Only a width change can change what fits; ignoring height changes
        # avoids a redraw loop (adding/removing "▾ N more" can nudge the height).
        if event.width != self._strip_last_width:
            self._strip_last_width = event.width
            self._schedule_strip_layout()

    def _schedule_strip_layout(self):
        """Coalesce redraws (status changes, resizes) into one after idle -- and
        never rebuild the buttons from inside one of their own click handlers."""
        if not hasattr(self, "server_strip") or self._strip_layout_pending:
            return
        self._strip_layout_pending = True
        self.root.after_idle(self._layout_strip)

    def _layout_strip(self):
        self._strip_layout_pending = False
        for w in self.server_strip.winfo_children():
            w.destroy()
        entries = self._strip_entries()
        selected = self._strip_selected_key()
        self._strip_choice.set(selected)
        buttons = []
        for e in entries:
            b = self.choice_button(self.server_strip, self._strip_choice, e["key"], kind="strip",
                                   text=self._strip_label(e), command=lambda k=e["key"]: self._on_strip_select(k))
            tip = self._strip_label(e, short=False)
            if e["online"] is not None:
                tip += f"\n{e['online']} player{'s' if e['online'] != 1 else ''} online"
            self.create_tooltip(b, tip)
            buttons.append((e, b))
        # Fit as many as the width allows; the rest go into "▾ N more". The
        # selected Server always stays visible (it takes the last visible slot).
        avail = self.server_strip.winfo_width()
        more = (self.button(self.server_strip, text="▾ 99 more") if self.look == "crafty"
                else ttk.Menubutton(self.server_strip, text="▾ 99 more"))
        more_w = more.winfo_reqwidth() + 4
        shown, used = [], 0
        for i, (e, b) in enumerate(buttons):
            w = b.winfo_reqwidth() + 4
            rest_needs_more = i < len(buttons) - 1
            if avail <= 1 or used + w + (more_w if rest_needs_more else 0) <= avail:
                shown.append((e, b)); used += w
            else:
                break
        hidden = [(e, b) for e, b in buttons if (e, b) not in shown]
        if hidden and selected and selected not in [e["key"] for e, _ in shown]:
            sel = next(eb for eb in hidden if eb[0]["key"] == selected)
            if shown:
                hidden.append(shown.pop())
            shown.append(sel); hidden.remove(sel)
            hidden.sort(key=lambda eb: eb[0]["number"])
        for _, b in shown:
            b.pack(side=tk.LEFT, padx=(0, 4))
        if hidden:
            more.config(text=f"▾ {len(hidden)} more")
            menu = tk.Menu(more, tearoff=False)
            for e, b in hidden:
                menu.add_command(label=self._strip_label(e, short=False),
                                 command=lambda k=e["key"]: self._on_strip_select(k))
                b.destroy()
            more["menu"] = menu
            more.pack(side=tk.LEFT)
        else:
            more.destroy()
        # "−" is only usable for local Servers when there is more than one.
        local_count = len(self.config.get("server_profiles", {}))
        remote_view = bool(entries and entries[0]["key"].startswith("r|"))
        self.server_remove_btn.config(state=tk.NORMAL if (local_count > 1 and not remote_view) else tk.DISABLED)
        self.server_add_btn.config(state=tk.DISABLED if remote_view else tk.NORMAL)
        # "✏" renames the selected local Server (its World, list name, server-name).
        self.server_rename_btn.config(state=tk.NORMAL if selected.startswith("l|") else tk.DISABLED)
        self._refresh_update_all_button()  # the Server count may have changed

    def _on_strip_select(self, key: str):
        kind, *rest = key.split("|")
        if kind == "l":
            pid = rest[0]
            if pid == self.config.get("active_profile") and not self.active_remote and not self._overview_kind:
                return
            if not self._confirm_leave_unsaved():
                self._schedule_strip_layout()  # put the highlight back
                return
            self._switch_to_profile(pid)
        else:
            self._select_remote_server(rest[0], rest[1])
        self._schedule_strip_layout()

    def _local_world_sources(self) -> List[Tuple[str, Path, bool]]:
        """(label, world folder, its Server is running) for every World of every
        local Server -- the choices for 'start from a copy of an existing World'."""
        out = []
        for n, (pid, profile) in enumerate(self.config.get("server_profiles", {}).items(), 1):
            worlds_dir = Path(profile.get("path", "")) / "worlds"
            ctx = self.contexts.get(pid)
            running = bool(ctx and ctx.is_running())
            try:
                for w in sorted(p for p in worlds_dir.iterdir() if p.is_dir()):
                    out.append((f"Server {n}: {w.name}", w, running))
            except OSError:
                pass
        return out

    def new_server_dialog(self):
        """'+': create a new, completely separate Server with the newest Bedrock
        Server Version -- no ZIP to find, no version to type (plan §2)."""
        if self.active_remote or self.is_updating:
            return
        if not self._confirm_leave_unsaved():
            return
        profiles = self.config.get("server_profiles", {})
        number = len(profiles) + 1
        dlg = tk.Toplevel(self.root)
        dlg.title("New Server")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"New Server (Server {number})", font=self.font(12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(body, text="Name players see:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        name_var = tk.StringVar(value=f"Server {number}")
        name_entry = ttk.Entry(body, textvariable=name_var, width=34)
        name_entry.grid(row=1, column=1, sticky="w", pady=3)

        ttk.Label(body, text="Game mode:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        gm_var = tk.StringVar(value="survival")
        gm = ttk.Frame(body); gm.grid(row=2, column=1, sticky="w", pady=3)
        for mode in ("survival", "creative", "adventure"):
            ttk.Radiobutton(gm, text=mode.capitalize(), value=mode, variable=gm_var).pack(side=tk.LEFT, padx=(0, 8))

        ttk.Label(body, text="Start with:").grid(row=3, column=0, sticky="ne", padx=(0, 6), pady=3)
        world_mode = tk.StringVar(value="new")
        wf = ttk.Frame(body); wf.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Radiobutton(wf, text="A new World", value="new", variable=world_mode).grid(row=0, column=0, sticky="w")
        seed_row = ttk.Frame(wf); seed_row.grid(row=1, column=0, sticky="w", padx=(20, 0), pady=(0, 4))
        seed_label = ttk.Label(seed_row, text="Seed (optional):")
        seed_label.pack(side=tk.LEFT)
        seed_var = tk.StringVar()
        seed_entry = ttk.Entry(seed_row, textvariable=seed_var, width=24)
        seed_entry.pack(side=tk.LEFT, padx=(6, 0))
        self.create_tooltip(seed_entry, "The same seed gives the same landscape.\nLeave it empty for a random World.")
        sources = self._local_world_sources()
        copy_rb = ttk.Radiobutton(wf, text="A copy of an existing World:", value="copy", variable=world_mode)
        copy_rb.grid(row=2, column=0, sticky="w")
        choices = [lbl + ("   (stop that Server first)" if running else "") for lbl, _, running in sources]
        src_var = tk.StringVar(value=choices[0] if choices else "")
        src_combo = ttk.Combobox(wf, textvariable=src_var, values=choices, state="readonly", width=40)
        src_combo.grid(row=3, column=0, sticky="w", padx=(20, 0))
        if not sources:
            copy_rb.config(state=tk.DISABLED); src_combo.config(state=tk.DISABLED)

        def on_world_mode(*_):
            # A seed only shapes a World Bedrock generates; a copy already has its landscape.
            new = world_mode.get() == "new"
            seed_entry.config(state=tk.NORMAL if new else tk.DISABLED)
            seed_label.config(foreground="" if new else self.fg("gray"))
        world_mode.trace_add("write", on_world_mode)

        notes = ["The newest Bedrock Server Version is downloaded and installed in its own folder."]
        if profiles:
            notes.append("Only one Server per computer can be in Minecraft's LAN list at a time — the one "
                         "started first. Players add the others under Servers → Add Server with this "
                         "computer's IP and port (shown when it's ready).")
        if sys.platform == "win32":
            notes.append("When it first starts, Windows will ask to allow bedrock_server.exe on the "
                         "network — choose Allow, or players can't connect.")
        # Only the first-started Server gets UDP 7551 (LAN discovery), so a ticked box
        # on several Servers is harmless: the others just aren't listed.
        lan_var = tk.BooleanVar(value=True)
        lan_box = self.check(body, text="Show in Minecraft's LAN list", variable=lan_var)
        lan_box.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(body, text="\n".join("• " + n for n in notes), foreground=self.fg("gray"), wraplength=460,
                  justify=tk.LEFT).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 6))
        status = ttk.Label(body, text="", foreground=self.fg("gray"), wraplength=460, justify=tk.LEFT)
        status.grid(row=6, column=0, columnspan=2, sticky="w")
        bar = ttk.Progressbar(body, maximum=100)
        bar.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 8))

        btns = ttk.Frame(body); btns.grid(row=8, column=0, columnspan=2, sticky="ew")
        existing = self.button(btns, text="Use an existing Bedrock folder instead…", style="Small.TButton",
                              command=lambda: (dlg.destroy(), self.add_server_profile()))
        existing.pack(side=tk.LEFT)
        cancel = self.button(btns, text="Cancel", command=dlg.destroy)
        cancel.pack(side=tk.RIGHT)
        create = self.button(btns, text="Create Server", style="Primary.TButton")
        create.pack(side=tk.RIGHT, padx=(0, 6))

        def set_status(text, pct=None, error=False):
            status.config(text=text, foreground=self.fg("#C62828") if error else "gray")
            if pct is not None:
                bar["value"] = pct

        def on_create():
            name = name_var.get().strip()
            if not name or any(c in name for c in ';/\\:*?"<>|'):
                set_status("Give the Server a name without ; / \\ : * ? \" < > |", error=True)
                return
            copy_from = None
            if world_mode.get() == "copy" and sources:
                idx = choices.index(src_var.get()) if src_var.get() in choices else -1
                if idx < 0:
                    set_status("Choose which World to copy.", error=True); return
                if sources[idx][2]:
                    set_status("That World's Server is running. Stop it first, so the copy is complete.", error=True)
                    return
                copy_from = sources[idx][1]
            for w in (name_entry, create, cancel, existing, src_combo, copy_rb, seed_entry, lan_box):
                w.config(state=tk.DISABLED)
            dlg.protocol("WM_DELETE_WINDOW", lambda: None)  # don't close mid-install
            self._create_server_async(dlg, set_status, name, gm_var.get(), copy_from,
                                      seed="" if copy_from else seed_var.get().strip(),
                                      lan_visible=lan_var.get())

        create.config(command=on_create)
        dlg.bind("<Return>", lambda e: on_create())
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        name_entry.focus_set(); name_entry.select_range(0, tk.END)
        dlg.grab_set()

    def _create_server_async(self, dlg, set_status, name: str, gamemode: str, copy_from: Optional[Path],
                             seed: str = "", lan_visible: bool = True):
        profiles = self.config.get("server_profiles", {})
        profile_id = uuid.uuid4().hex[:8]
        # Next to Server 1's folder; a first-ever Server goes under the home folder.
        first = next(iter(profiles.values()), None)
        parent = Path(first["path"]).parent if first else Path.home() / "Bedrock Servers"
        target = parent / f"server-{profile_id}"  # permanent, never shown, never renamed
        used = set()
        for p in profiles.values():
            props = parse_server_properties(Path(p.get("path", "")) / "server.properties")
            for key in ("server-port", "server-portv6"):
                if props.get(key, "").isdigit():
                    used.add(int(props[key]))
        first_server = not profiles  # gets Bedrock's standard port 19132
        ui = lambda fn: self.root.after(0, fn)

        def worker():
            try:
                rel = self._latest_release
                if not rel:
                    ui(lambda: set_status("Asking Mojang for the newest version…"))
                    rel = fetch_latest_bedrock_release()
                dl = Path(get_downloads_folder()); dl.mkdir(parents=True, exist_ok=True)
                zpath = download_bedrock_zip(rel["url"], dl, progress_callback=lambda p: ui(
                    lambda p=p: set_status(f"Downloading {rel['version']}… {p:.0f}%", p * 0.8)))
                # A first-ever Server gets Bedrock's standard port; later ones the next free pair.
                port = find_free_server_port(used, start=19132 if first_server else NEW_SERVER_FIRST_PORT)
                ui(lambda: set_status(f"Installing {rel['version']}…", 85))
                create_server_folder(zpath, target, server_name=name, gamemode=gamemode, port=port,
                                     lan_visible=lan_visible, world_name=name, copy_world_from=copy_from,
                                     version=rel["version"], seed=seed)
            except Exception as e:
                _LOG.exception("Creating a new Server failed")
                shutil.rmtree(_long_path(target), ignore_errors=True)  # never leave a half-made Server
                def failed(e=e):
                    set_status(f"Couldn't create the Server: {e}", error=True)
                    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
                    self.button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 10))
                ui(failed)
                return
            ui(lambda: self._finish_new_server(dlg, profile_id, name, target, port, lan_visible, rel["version"]))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_new_server(self, dlg, profile_id, name, target: Path, port: int, lan_visible: bool, version):
        self._sync_flat_settings_into_active_profile()
        self.config.setdefault("server_profiles", {})[profile_id] = {
            "name": name,
            "path": str(target),
            "preserve_items": copy.deepcopy(DEFAULT_PRESERVE_ITEMS),
            "max_backups": 5,
            "compress_backups": False,
            "auto_cleanup_backups": True,
            "auto_stop_server_before_update": True,
            "auto_start_server_after_update": False,
            "known_players": {},
        }
        save_config(self.config)
        self._announce_servers_changed()
        try:
            dlg.grab_release(); dlg.destroy()
        except tk.TclError:
            pass
        self._switch_to_profile(profile_id)
        self.notebook.select(self.properties_editor)  # review before the first start
        self.properties_editor.load_properties()
        self.log(f"New Server '{name}' created with Bedrock Server Version {version} in {target} (port {port}).", "success")
        add_by_hand = (f"Players add it in Minecraft under Servers → Add Server:\n"
                       f"    Address: {get_local_ip()}\n    Port: {port}")
        others = len(self.config.get("server_profiles", {})) > 1
        if lan_visible and not others:
            how = "It shows up in Minecraft's LAN list."
        elif lan_visible:
            how = ("It shows up in Minecraft's LAN list when it's the first Server started on this "
                   "computer (only one can be listed at a time). Otherwise:\n" + add_by_hand)
        else:
            how = add_by_hand
        messagebox.showinfo("Server created",
            f"'{name}' is ready (not started yet).\n\n{how}\n\n"
            "Review its settings here, then press Start on the 🎮 Server tab.")

    def rename_server_dialog(self):
        """'✏': rename the selected local Server in one place. The top line shows
        its Active World, the sidebar/Fleet its list name, and players see
        `server-name` -- one name for all three, with server-name optional."""
        key = self._strip_selected_key()
        if not key.startswith("l|") or self.active_remote or self.is_updating:
            return
        pid = key[2:]
        profile = self.config.get("server_profiles", {}).get(pid)
        acc = self.contexts.get(pid) or self._ensure_service(pid)
        if profile is None or acc is None:
            return
        number = next((e["number"] for e in self._strip_entries() if e["key"] == key), 1)
        try:
            props = acc.read_properties()
        except Exception:
            props = {}
        old_world = props.get("level-name", "") or profile.get("name", "")
        old_players_name = props.get("server-name", "")
        dlg = tk.Toplevel(self.root)
        dlg.title("Rename Server")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=f"Rename Server {number}", font=self.font(12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(body, text="New name:").grid(row=1, column=0, sticky="e", padx=(0, 6))
        name_var = tk.StringVar(value=old_world)
        entry = ttk.Entry(body, textvariable=name_var, width=34)
        entry.grid(row=1, column=1, sticky="w")
        # Follow along by default only while players still see the stock name or
        # the old one -- a server-name the user chose on purpose is left alone.
        players_var = tk.BooleanVar(value=old_players_name.strip() in ("", "Dedicated Server", old_world,
                                                                      profile.get("name", "")))
        self.check(body, variable=players_var,
                        text=f"Also the name players see in their server list (now: “{old_players_name or '—'}”)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        status = ttk.Label(body, text="", foreground=self.fg("#E65100"), wraplength=420, justify=tk.LEFT)
        status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        rename_btn = self.button(btns, text="Rename", style="Primary.TButton")
        rename_btn.pack(side=tk.RIGHT, padx=(0, 6))
        if acc.is_running():
            # Its World folder is renamed too, which can't happen under a running engine.
            rename_btn.config(state=tk.DISABLED)
            status.config(text="Stop this Server first — its World folder is renamed too.")

        def do_rename(_=None):
            if str(rename_btn.cget("state")) == "disabled":
                return
            new = name_var.get().strip()
            if not new or any(c in new for c in ';/\\:*?"<>|'):
                status.config(text="Use a name without ; / \\ : * ? \" < > |", foreground=self.fg("#C62828"))
                return
            if acc.is_running():
                status.config(text="Stop this Server first — its World folder is renamed too.", foreground=self.fg("#E65100"))
                return
            try:
                self._rename_server(pid, acc, old_world, new, players_var.get())
            except Exception as e:
                status.config(text=f"Couldn't rename it: {e}", foreground=self.fg("#C62828"))
                return
            dlg.destroy()

        rename_btn.config(command=do_rename)
        dlg.bind("<Return>", do_rename)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        entry.focus_set(); entry.select_range(0, tk.END)
        dlg.grab_set()

    def _rename_server(self, pid: str, acc, old_world: str, new: str, players_too: bool):
        """Rename the World (folder + level-name), the list name and, optionally,
        server-name. Raises with a plain message if the World can't be renamed."""
        existing = {w["name"] for w in acc.list_worlds()}
        if new != old_world:
            clash = next((w for w in existing if w.lower() == new.lower() and w.lower() != old_world.lower()), None)
            if clash:
                raise ValueError(f"this Server already has a World called “{clash}”")
            if old_world in existing:
                if new.lower() == old_world.lower():
                    # Only the capitals change: go through a temporary name, since
                    # Windows sees "Kids" and "kids" as the same folder.
                    tmp = f"{old_world}.renaming"
                    if not (acc.rename_world(old_world, tmp) and acc.rename_world(tmp, new)):
                        raise OSError("the World folder couldn't be renamed")
                elif not acc.rename_world(old_world, new):
                    raise OSError("the World folder couldn't be renamed")
            else:
                acc.set_active_world(new)  # not generated yet: only the pointer exists
        if players_too:
            acc.write_properties({"server-name": new})
        self.config["server_profiles"][pid]["name"] = new
        save_config(self.config)
        self._announce_servers_changed()
        self.log(f"Renamed Server '{old_world}' to '{new}'" + (" (players see the new name too)" if players_too else ""),
                 "success")
        if self.config.get("active_profile") == pid and not self.active_remote:
            self.refresh_worlds()
            self.refresh_world_combo()
            self.update_server_info()
            if hasattr(self, 'properties_editor') and not self.properties_editor.has_unsaved_changes():
                self.properties_editor.load_properties()
        self.refresh_sidebar()

    def remove_server_dialog(self):
        """'−': pick ANY local Server (nothing preselected), then a typed-name
        confirmation that lists exactly what will be deleted (plan §3)."""
        if self.active_remote:
            return
        if self.is_updating:
            messagebox.showinfo("Remove a Server", "Wait until the update has finished.")
            return
        entries = [e for e in self._strip_entries() if e["key"].startswith("l|")]
        if len(entries) <= 1:
            messagebox.showinfo("Remove a Server", "You can't remove your only Server.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Remove a Server")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        choice = tk.StringVar(value="")

        def clear():
            for w in body.winfo_children():
                w.destroy()

        def pick_page():
            clear()
            ttk.Label(body, text="Which Server do you want to remove?",
                      font=self.font(12, "bold")).pack(anchor="w", pady=(0, 8))
            for e in entries:
                ctx = self.contexts.get(e["key"][2:])
                online = len(ctx.online_players()) if ctx else 0
                text = self._strip_label(e, short=False)
                rb = ttk.Radiobutton(body, text=text, value=e["key"], variable=choice,
                                     command=lambda: nxt.config(state=tk.NORMAL))
                if online:
                    rb.config(text=f"{text}   — {online} player{'s' if online != 1 else ''} online", state=tk.DISABLED)
                rb.pack(anchor="w", pady=1)
            row = ttk.Frame(body); row.pack(fill=tk.X, pady=(12, 0))
            self.button(row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
            nxt = self.button(row, text="Next…", state=tk.DISABLED, command=confirm_page)
            nxt.pack(side=tk.RIGHT, padx=(0, 6))
            if choice.get():
                nxt.config(state=tk.NORMAL)

        def confirm_page():
            e = next(x for x in entries if x["key"] == choice.get())
            pid = e["key"][2:]
            profile = self.config["server_profiles"][pid]
            path = Path(profile["path"])
            ctx = self.contexts.get(pid)
            try:
                worlds = sorted(p.name for p in (path / "worlds").iterdir() if p.is_dir())
            except OSError:
                worlds = []
            bdir = server_backup_dir(path)
            try:
                n_backups = sum(1 for p in bdir.iterdir() if p.name.startswith("backup_"))
            except OSError:
                n_backups = 0
            clear()
            title = self._strip_label(e, short=False)[2:]  # without the dot
            ttk.Label(body, text=f"Permanently delete {title}?", font=self.font(12, "bold"),
                      foreground=self.fg("#C62828")).pack(anchor="w", pady=(0, 8))
            lines = [f"Its folder: {path}",
                     f"Worlds: {', '.join(worlds) if worlds else '(none yet)'}",
                     f"Backups: {n_backups} (in {bdir})"]
            ttk.Label(body, text="This deletes, and it can't be undone:\n" + "\n".join("   • " + l for l in lines),
                      justify=tk.LEFT, wraplength=520).pack(anchor="w")
            if ctx and ctx.is_running():
                ttk.Label(body, text="It's running and will be stopped first.", foreground=self.fg("#E65100")).pack(anchor="w", pady=(6, 0))
            ttk.Label(body, text=f"\nType  {e['world']}  to confirm:").pack(anchor="w")
            typed = tk.StringVar()
            entry = ttk.Entry(body, textvariable=typed, width=40); entry.pack(anchor="w", pady=(2, 8))
            status = ttk.Label(body, text="", foreground=self.fg("gray"), wraplength=520, justify=tk.LEFT)
            status.pack(anchor="w")
            row = ttk.Frame(body); row.pack(fill=tk.X, pady=(8, 0))
            self.button(row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
            delete = self.button(row, text="Delete permanently", style="Danger.TButton", state=tk.DISABLED,
                                command=lambda: self._remove_server_async(dlg, status, pid, e))
            delete.pack(side=tk.RIGHT, padx=(0, 6))
            self.button(row, text="◀ Back", command=pick_page).pack(side=tk.LEFT)
            typed.trace_add("write", lambda *a: delete.config(
                state=tk.NORMAL if typed.get().strip() == e["world"] else tk.DISABLED))
            entry.focus_set()

        pick_page()
        dlg.bind("<Escape>", lambda ev: dlg.destroy())
        dlg.grab_set()

    def _remove_server_async(self, dlg, status, pid: str, entry: dict):
        ctx = self.contexts.get(pid)
        if ctx and ctx.online_players():  # someone joined meanwhile
            status.config(text="Players are online now — try again when they've left.", foreground=self.fg("#C62828"))
            return
        for w in dlg.winfo_children():
            for c in w.winfo_children():
                try:
                    c.config(state=tk.DISABLED)
                except tk.TclError:
                    pass
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        path = Path(self.config["server_profiles"][pid]["path"])
        status.config(text="Stopping…" if ctx and ctx.is_running() else "Deleting…", foreground=self.fg("gray"))

        def worker():
            leftovers, refused = [], None
            try:
                if ctx and ctx.is_running():
                    ctx.stop()
                    for _ in range(60):  # the engine releases its files only once it has exited
                        if not ctx.is_running():
                            break
                        time.sleep(0.5)
                self.root.after(0, lambda: status.config(text="Deleting…"))
                leftovers = delete_server_files(path)
            except ValueError as e:
                refused = str(e)
            except Exception as e:
                _LOG.exception("Removing a Server failed")
                refused = f"Deleting failed: {e}"
            self.root.after(0, lambda: self._finish_remove_server(dlg, pid, entry, path, leftovers, refused))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_remove_server(self, dlg, pid, entry, path: Path, leftovers: List[str], refused: Optional[str]):
        label = self._strip_label(entry, short=False)[2:]
        self.config["server_profiles"].pop(pid, None)
        self.contexts.pop(pid, None)
        if self.config.get("active_profile") == pid or not self.config.get("active_profile"):
            first = next(iter(self.config["server_profiles"]), None)
            self.config["active_profile"] = None
            # Empty the Server Folder field first: switching saves the "current"
            # Server, and with the deleted folder still in it, that save would
            # quietly re-create a list entry for the Server just removed.
            self.server_entry.delete(0, tk.END)
            if first:
                self._switch_to_profile(first)
        save_config(self.config)
        self._announce_servers_changed()
        self.refresh_sidebar()
        try:
            dlg.grab_release(); dlg.destroy()
        except tk.TclError:
            pass
        if refused:
            self.log(f"Removed {label} from the list; its files were not deleted: {refused}", "warning")
            messagebox.showwarning("Server removed from the list",
                f"{label} is no longer listed, but its files were NOT deleted:\n\n{refused}")
        elif leftovers:
            self.log(f"Removed {label}; some files couldn't be deleted: {leftovers[:5]}", "warning")
            messagebox.showwarning("Server removed",
                f"{label} was removed, but these couldn't be deleted (delete them by hand):\n\n"
                + "\n".join(leftovers[:10]) + ("\n…" if len(leftovers) > 10 else ""))
        else:
            self.log(f"Removed {label} and deleted its folder {path} and its backups.", "success")
            messagebox.showinfo("Server removed", f"{label} and its backups were deleted.")

    def refresh_sidebar(self):
        """Rebuild the Machines/Servers tree from config + live registry/connection state."""
        if not hasattr(self, 'sidebar_tree'):
            return
        self._sidebar_refreshing = True
        try:
            self.sidebar_tree.delete(*self.sidebar_tree.get_children())
            # Fleet: every Server across every Machine, one place to see/start/stop all of them.
            self.sidebar_tree.insert("", 0, iid="fleet", text="🌐 Fleet (All Servers)")
            # This computer + its local Server profiles.
            local_node = self.sidebar_tree.insert("", tk.END, iid="machine:local",
                                                   text="🖥 This computer", open=True)
            active_id = self.config.get("active_profile")
            for e in self._machine_server_entries():
                self.sidebar_tree.insert(local_node, tk.END, iid=f"profile:{e['key'][2:]}",
                                         text=self._strip_label(e, short=False))
            # Remote Machines + their Servers.
            for machine in self.config.get("machines", []):
                mid = machine.get("id")
                st = self._remote_state.get(mid, {})
                connected = st.get("connected", False)
                mdot = "🖥" if connected else "🖥🔴"
                mnode = self.sidebar_tree.insert("", tk.END, iid=f"rmachine:{mid}",
                                                 text=f"{mdot} {machine.get('name', machine.get('host'))}",
                                                 open=True)
                if not connected:
                    self.sidebar_tree.insert(mnode, tk.END, iid=f"rinfo:{mid}",
                                             text="   (connecting…)")
                    continue
                for e in self._machine_server_entries(mid):
                    sid = e["key"].split("|", 2)[2]
                    self.sidebar_tree.insert(mnode, tk.END, iid=f"rprofile:{mid}:{sid}",
                                             text=self._strip_label(e, short=False))
            # Restore selection highlight.
            sel_iid = None
            if self.active_remote:
                sel_iid = f"rprofile:{self.active_remote[0]}:{self.active_remote[1]}"
            elif active_id:
                sel_iid = f"profile:{active_id}"
            if sel_iid:
                try:
                    self.sidebar_tree.selection_set(sel_iid)
                except tk.TclError:
                    pass
            elif self._overview_kind:
                if self._overview_kind[0] == "fleet":
                    ov_iid = "fleet"
                elif self._overview_kind[1] == "local":
                    ov_iid = "machine:local"
                else:
                    ov_iid = f"rmachine:{self._overview_kind[2]}"
                try:
                    self.sidebar_tree.selection_set(ov_iid)
                except tk.TclError:
                    pass
        finally:
            self._sidebar_refreshing = False
        self._schedule_strip_layout()  # the top-line Server strip mirrors the same state
        # Keep whatever overview page is on screen live (status dots etc.).
        if self._overview_kind:
            if self._overview_kind[0] == "fleet":
                self.build_fleet_overview()
            else:
                self.build_machine_page(self._overview_kind[1], self._overview_kind[2])

    def on_sidebar_select(self, event=None):
        if getattr(self, "_sidebar_refreshing", False):
            return  # ignore selection changes we caused by rebuilding the tree
        sel = self.sidebar_tree.selection()
        if not sel:
            return
        iid = sel[0]

        # A local Server profile.
        if iid.startswith("profile:"):
            new_id = iid.split(":", 1)[1]
            # Even if this IS already the active profile, still proceed if we're
            # currently showing an overview page (Fleet/Machine) -- otherwise
            # clicking back to the already-active Server would do nothing.
            if new_id == self.config.get("active_profile") and not self.active_remote and not self._overview_kind:
                return
            if not self._confirm_leave_unsaved():
                self.refresh_sidebar()
                return
            self._switch_to_profile(new_id)
            return

        # A remote Server.
        if iid.startswith("rprofile:"):
            _, mid, sid = iid.split(":", 2)
            if self.active_remote == (mid, sid) and not self._overview_kind:
                return
            self._select_remote_server(mid, sid)
            return

        # The Fleet root: every Server across every Machine.
        # (Each overview also redraws the top line: no single Server is selected
        # there, and a remote Machine's page shows that Machine's Servers.)
        if iid == "fleet":
            self._overview_kind = ("fleet",)
            self.show_overview()
            self.build_fleet_overview()
            self._schedule_strip_layout()
            return

        # "This computer" -- a local Machine page.
        if iid == "machine:local":
            self._overview_kind = ("machine", "local", "local")
            self.show_overview()
            self.build_machine_page("local", "local")
            self._schedule_strip_layout()
            return

        # A remote Machine node, or its "(connecting…)" placeholder.
        if iid.startswith("rmachine:") or iid.startswith("rinfo:"):
            mid = iid.split(":", 1)[1]
            self._overview_kind = ("machine", "remote", mid)
            self.show_overview()
            self.build_machine_page("remote", mid)
            self._schedule_strip_layout()
            return

    def _confirm_leave_unsaved(self) -> bool:
        if hasattr(self, 'properties_editor') and self.properties_editor.has_unsaved_changes():
            return messagebox.askyesno("Unsaved changes",
                "The Active Server Configuration has unsaved changes.\n\n"
                "Discard them and switch Servers?")
        return True

    def _switch_to_profile(self, profile_id: str):
        """Point every tab at a different Server. See docs/V2-MAJORDOMO-PLAN.md,
        'GUI: sidebar + the same 7 tabs' -- a still-running previous Server is
        NOT stopped; it keeps running in the registry, which is the point."""
        self._overview_kind = None
        self.show_notebook()
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

    # ==================================================================
    # Remote Machines: connections, events, selection (Stage 3d)
    # ==================================================================
    def _ensure_connections(self):
        """Open (and keep) a MachineConnection for every configured Machine."""
        for machine in self.config.get("machines", []):
            mid = machine.get("id")
            if mid and mid not in self.connections:
                self._start_connection(machine)
        self.refresh_sidebar()

    def _start_connection(self, machine: dict):
        mid = machine["id"]
        self._remote_state.setdefault(mid, {"connected": False, "servers": [], "name": machine.get("name")})
        # Callbacks run on the connection's worker thread -> only enqueue; the
        # main-thread poller (_drain_remote_queue) does the actual UI work.
        conn = MachineConnection(
            machine,
            on_event=lambda ev, m=mid: self._remote_queue.put(("event", m, ev)),
            on_state=lambda c, d, m=mid: self._remote_queue.put(("state", m, c, d)),
            log=lambda msg, m=mid: self._remote_queue.put(("log", m, msg)))
        self.connections[mid] = conn
        conn.start()

    def _drain_remote_queue(self):
        """Runs on the tkinter main thread: apply everything the connection
        workers enqueued, then reschedule itself."""
        try:
            while True:
                item = self._remote_queue.get_nowait()
                kind = item[0]
                # Per item, so one failing event can't hold up the rest.
                try:
                    if kind == "event":
                        self._on_remote_event(item[1], item[2])
                    elif kind == "state":
                        self._on_remote_state(item[1], item[2], item[3])
                    elif kind == "log":
                        self.log(item[2], "info")
                except Exception:
                    self.logger.exception("Could not apply remote %s update", kind)
        except queue.Empty:
            pass
        finally:
            self.root.after(150, self._drain_remote_queue)

    def _stop_connection(self, machine_id: str):
        conn = self.connections.pop(machine_id, None)
        if conn:
            conn.close()
        self._remote_state.pop(machine_id, None)

    def _on_remote_state(self, machine_id: str, connected: bool, detail: str):
        st = self._remote_state.setdefault(machine_id, {"connected": False, "servers": [], "name": machine_id})
        st["connected"] = connected
        conn = self.connections.get(machine_id)
        if connected and conn:
            st["servers"] = conn.machine_info.get("servers", [])
            st["name"] = conn.machine_info.get("name", st.get("name"))
        # If the Server on-screen is on this Machine, reflect the link state.
        if self.active_remote and self.active_remote[0] == machine_id:
            self.update_server_status("running" if (connected and self.active_access
                                                     and self.active_access.is_running()) else "stopped")
        self.refresh_sidebar()

    def _on_remote_event(self, machine_id: str, ev: dict):
        etype = ev.get("event")
        sid = ev.get("server")
        viewing = (self.active_remote == (machine_id, sid))
        if etype == "servers_changed":
            self._remote_state.setdefault(machine_id, {})["servers"] = ev.get("data", {}).get("servers", [])
            self.refresh_sidebar()
        elif etype == "console" and viewing:
            for line in ev.get("data", {}).get("lines", []):
                self.console_log(line)
                self._check_transport_warning(line)
                self._check_console_replies(line)
        elif etype == "status":
            running = ev.get("data", {}).get("running", False)
            if viewing and self.active_access:
                self.active_access.note_status(running)
                self.update_server_status("running" if running else "stopped")
            self.refresh_sidebar()
        elif etype == "progress" and viewing:
            data = ev.get("data", {})
            pct = data.get("percent", 0)
            self.set_progress(pct, f"{data.get('op', 'working')}… {pct:.0f}%")

    def _select_remote_server(self, machine_id: str, server_id: str):
        conn = self.connections.get(machine_id)
        if conn is None or not conn.is_connected():
            messagebox.showinfo("Not connected",
                "That Machine isn't connected right now. It will keep trying in the background.")
            self.refresh_sidebar()
            return
        if self.active_access is not None and self.properties_editor.has_unsaved_changes():
            if not messagebox.askyesno("Unsaved changes",
                    "The Active Server Configuration has unsaved changes.\n\nDiscard them and switch Servers?"):
                self.refresh_sidebar()
                return
        self._overview_kind = None
        self.show_notebook()
        self.active_access = RemoteServerAccess(conn, server_id)
        self.active_remote = (machine_id, server_id)
        self.config["active_profile"] = None  # a remote Server is selected, not a local profile
        # Replay the remote console buffer into the widget.
        if self.layout == "menu":
            try:
                self._console_lines = list(self.active_access.console_snapshot())
            except Exception:
                self._console_lines = []
            self._rerender_console()
        else:
            self.console_text.config(state=tk.NORMAL)
            self.console_text.delete(1.0, tk.END)
            try:
                for line in self.active_access.console_snapshot():
                    self.console_text.insert(tk.END, line + "\n")
            except Exception:
                pass
            self.console_text.see(tk.END)
            self.console_text.config(state=tk.DISABLED)
        # Seed running state from the Machine's last-reported server list
        # (authoritative + synchronous, no round-trip); update_server_info()
        # below re-confirms it from a fresh get_info(). Without this the widget
        # defaults to "stopped" until the user presses Start -- wrong for a
        # remote Server that's been running on the host for hours and is never
        # started from here (status-change events only fire on start/stop, so
        # a long-running server never announces itself to a late-joining admin).
        running = self._remote_server_running(machine_id, server_id)
        self.active_access.note_status(running)
        try:
            self.update_server_status("running" if running else "stopped")
            self.update_server_info()
        except Exception as e:
            self.log(f"Could not load remote Server: {e}", "error")
        self._refresh_remote_dependent_tabs()
        self.refresh_sidebar()

    def _remote_server_running(self, machine_id: str, server_id: str) -> bool:
        """The host's last-reported running state for one of its Servers, from
        the cached machine server list (populated on connect and refreshed by
        'servers_changed' events) -- no network round-trip."""
        for sv in self._remote_state.get(machine_id, {}).get("servers", []):
            if sv.get("id") == server_id:
                return bool(sv.get("running"))
        return False

    def _refresh_remote_dependent_tabs(self):
        """After selecting a remote Server, refresh the tabs that have been
        routed through the access object; the rest show a remote placeholder."""
        try:
            self.refresh_worlds()
            self.refresh_players_tab()
            self.refresh_backups()
        except Exception:
            pass

    def _acc(self):
        """The Server currently on screen as a uniform access object, or None."""
        return self.active_access

    def _block_if_remote(self, what: str = "This action") -> bool:
        """Guard for actions that are either physically local (opening a folder
        on disk) or deliberately kept local-only for safety: running an Update
        wipes and replaces the entire Server install, and doing that blind over
        a network link -- where a dropped connection mid-copy could leave a
        Server half-wiped with no one there to notice -- is a risk not worth
        taking. See docs/V2-MAJORDOMO-PLAN.md, 'Deferred (deliberately local)'.
        Returns True (and tells the user) when a remote Server is selected."""
        if self.active_remote:
            messagebox.showinfo("Local-only",
                f"{what} runs on the machine that hosts the Server, so it isn't available "
                "from here for a remote Server.\n\nDo it on that machine directly. Status, "
                "console, start/stop, commands, worlds (incl. rename/delete), players, "
                "configuration and backups all work remotely.")
            return True
        return False

    def add_machine_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("Add Machine")
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        fields = {}
        rows = [("Name", "name", ""), ("Host / IP", "host", ""),
                ("Port", "port", str(REMOTE_DEFAULT_PORT)), ("Pairing token", "token", "")]
        for i, (label, key, default) in enumerate(rows):
            ttk.Label(frm, text=label + ":").grid(row=i, column=0, sticky="e", padx=(0, 6), pady=3)
            var = tk.StringVar(value=default)
            ttk.Entry(frm, textvariable=var, width=28).grid(row=i, column=1, sticky="w", pady=3)
            fields[key] = var
        status = ttk.Label(frm, text="LAN only — enter the address + token shown on that PC's Settings.",
                           font=self.font(8), foreground=self.fg("gray"))
        status.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 4))

        def do_test(save_after=False):
            host = fields["host"].get().strip()
            token = fields["token"].get().strip()
            try:
                port = int(fields["port"].get())
            except ValueError:
                status.config(text="Port must be a number.", foreground=self.fg("#F44336"))
                return
            if not host or not token:
                status.config(text="Host and token are required.", foreground=self.fg("#F44336"))
                return
            status.config(text="Testing…", foreground=self.fg("gray"))
            dlg.update_idletasks()

            def worker():
                try:
                    conn, info = remote_connect(host, port, token, timeout=6)
                    conn.close()
                    self.root.after(0, lambda: on_ok(info))
                except Exception as e:
                    self.root.after(0, lambda e=e: status.config(text=f"✗ {e}", foreground=self.fg("#F44336")))

            def on_ok(info):
                status.config(text=f"✓ Connected to {info.get('name', host)} "
                                   f"({len(info.get('servers', []))} Server(s)).", foreground=self.fg("#4CAF50"))
                if save_after:
                    m = self.add_machine(fields["name"].get(), host, port, token)
                    self._start_connection(m)
                    self.refresh_sidebar()
                    dlg.destroy()
            threading.Thread(target=worker, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, pady=(8, 0))
        self.button(btns, text="Test connection", command=lambda: do_test(False)).pack(side=tk.LEFT, padx=3)
        self.button(btns, text="Save", command=lambda: do_test(True), style="Primary.TButton").pack(side=tk.LEFT, padx=3)
        self.button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=3)

    # ==================================================================
    # Fleet overview + Machine page (Stage 4a) -- shown INSTEAD of the
    # notebook when the sidebar's Fleet root or a Machine node is selected.
    # ==================================================================
    def show_notebook(self):
        self.overview_frame.pack_forget()
        if getattr(self, "_tabbar", None) is not None:
            self.notebook.pack_forget()
            self._tabbar.pack(fill=tk.X)
            self._refresh_menu_highlight()
        self.notebook.pack(fill=tk.BOTH, expand=True)
        if self.layout == "menu" and str(self.left_menu) not in self.main_pane.panes():
            self.main_pane.insert(0, self.left_menu, weight=0)
            self._refresh_menu_highlight()

    def show_overview(self):
        self.notebook.pack_forget()
        if getattr(self, "_tabbar", None) is not None:
            self._tabbar.pack_forget()
        self.overview_frame.pack(fill=tk.BOTH, expand=True)
        if self.layout == "menu" and str(self.left_menu) in self.main_pane.panes():
            self.main_pane.forget(self.left_menu)   # Machines is a page of its own

    def _fleet_rows_for(self, only_machine=None):
        """Yield (iid, machine_label, server_label, running, row_info) for
        every Server, optionally filtered to one Machine ('local' or a
        machine_id). row_info is what _fleet_access_for_row() consumes."""
        if only_machine in (None, "local"):
            for e in self._machine_server_entries():
                pid = e["key"][2:]
                yield (f"floc:{pid}", "🖥 This computer", f"Server {e['number']}: {e['world']}",
                       e["running"], ("local", pid))
        if only_machine != "local":
            for machine in self.config.get("machines", []):
                mid = machine.get("id")
                if only_machine is not None and mid != only_machine:
                    continue
                st = self._remote_state.get(mid, {})
                mlabel = f"🖥 {st.get('name') or machine.get('name', machine.get('host'))}"
                if not st.get("connected"):
                    yield (f"frem:{mid}:_", mlabel, "(not connected)", False, None)
                    continue
                for e in self._machine_server_entries(mid):
                    sid = e["key"].split("|", 2)[2]
                    yield (f"frem:{mid}:{sid}", mlabel, f"Server {e['number']}: {e['world']}",
                           e["running"], ("remote", mid, sid))

    def _build_server_list_tree(self, parent, show_machine_column: bool):
        columns = ("machine", "server", "status") if show_machine_column else ("server", "status")
        tree = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        if show_machine_column:
            tree.heading("machine", text="Machine")
            tree.column("machine", width=170)
        tree.heading("server", text="Server")
        tree.heading("status", text="Status")
        tree.column("server", width=230)
        tree.column("status", width=110, anchor="center")
        tree.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
        tree.bind("<Double-1>", self._fleet_row_double_click)
        return tree

    def build_fleet_overview(self):
        for w in self.overview_frame.winfo_children():
            w.destroy()
        if self.layout == "menu":   # the Machines page of the New layout
            head = ttk.Frame(self.overview_frame)
            head.pack(fill=tk.X, pady=(0, 6))
            self.button(head, text="‹ Back", command=self._leave_overview).pack(side=tk.LEFT)
            self.button(head, text="+ Add Machine", style="Primary.TButton",
                       command=self.add_machine_dialog).pack(side=tk.RIGHT)
            ttk.Label(self.overview_frame, text="MACHINES", font=crafty_font("big")).pack(anchor="w")
            ttk.Label(self.overview_frame, text="Every computer and its Servers in one place",
                      style="Dim.TLabel").pack(anchor="w", pady=(2, 6))
        else:
            ttk.Label(self.overview_frame, text="🌐 Fleet — every Server, every Machine",
                      font=self.font(14, "bold")).pack(anchor="w")
        ttk.Label(self.overview_frame, text="Double-click a row to open it; select + use the buttons to start/stop.",
                  font=self.font(8), foreground=self.fg("gray")).pack(anchor="w", pady=(0, 4))
        tree = self._build_server_list_tree(self.overview_frame, show_machine_column=True)
        self._fleet_tree = tree
        self._fleet_rows = {}
        for iid, mlabel, slabel, running, info in self._fleet_rows_for():
            tree.insert("", tk.END, iid=iid, values=(mlabel, slabel, "🟢 Running" if running else "⚪ Stopped"))
            if info:
                self._fleet_rows[iid] = info
        self._build_fleet_action_buttons(self.overview_frame)

    def build_machine_page(self, kind: str, machine_id: str):
        for w in self.overview_frame.winfo_children():
            w.destroy()
        header = ttk.Frame(self.overview_frame)
        header.pack(fill=tk.X, pady=(0, 4))
        if kind == "local":
            title, subtitle = "🖥 This computer", f"{APP_NAME} {APP_VERSION_LABEL}   |   platform: {sys.platform}"
        else:
            machine = next((m for m in self.config.get("machines", []) if m["id"] == machine_id), None)
            st = self._remote_state.get(machine_id, {})
            conn = self.connections.get(machine_id)
            connected = bool(conn and conn.is_connected())
            title = f"🖥 {st.get('name') or (machine or {}).get('name', 'Machine')}"
            bits = [f"{(machine or {}).get('host', '?')}:{(machine or {}).get('port', '?')}",
                    "🟢 Connected" if connected else "🔴 Not connected"]
            info = conn.machine_info if conn else {}
            if info.get("platform"):
                bits.append(f"platform: {info['platform']}")
            if info.get("app_version"):
                bits.append(f"v{info['app_version']}")
            subtitle = "   |   ".join(bits)
        ttk.Label(header, text=title, font=self.font(14, "bold")).pack(anchor="w")
        ttk.Label(header, text=subtitle, foreground=self.fg("gray")).pack(anchor="w")

        tree = self._build_server_list_tree(self.overview_frame, show_machine_column=False)
        self._fleet_tree = tree
        self._fleet_rows = {}
        for iid, _mlabel, slabel, running, info in self._fleet_rows_for(only_machine=machine_id):
            tree.insert("", tk.END, iid=iid, values=(slabel, "🟢 Running" if running else "⚪ Stopped"))
            if info:
                self._fleet_rows[iid] = info
        self._build_fleet_action_buttons(self.overview_frame, machine_id=(machine_id if kind == "remote" else None))

    def _build_fleet_action_buttons(self, parent, machine_id=None):
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, pady=(10, 0))
        self.button(btns, text="▶️ Start Selected", command=self.fleet_start_selected).pack(side=tk.LEFT, padx=3)
        self.button(btns, text="⏹️ Stop Selected", command=self.fleet_stop_selected).pack(side=tk.LEFT, padx=3)
        if machine_id is not None:
            self.button(btns, text="🔌 Remove Machine",
                       command=lambda: self.remove_machine_confirm(machine_id)).pack(side=tk.RIGHT, padx=3)
        self.button(btns, text="🔄 Refresh", command=lambda: self.refresh_sidebar()).pack(side=tk.RIGHT, padx=3)

    def _fleet_access_for_row(self, iid):
        """Returns (access_object_or_None, display_name) for a fleet/machine-page row."""
        info = self._fleet_rows.get(iid)
        if not info:
            return None, None
        if info[0] == "local":
            pid = info[1]
            profile = self.config.get("server_profiles", {}).get(pid, {})
            ctx = self.contexts.get(pid)
            if ctx is None and profile.get("path") and Path(profile["path"]).exists():
                ctx = self._build_context(pid, Path(profile["path"]))
                self.contexts[pid] = ctx
            return ctx, self._server_display_name(None, f"l|{pid}")
        _, mid, sid = info
        conn = self.connections.get(mid)
        if conn is None or not conn.is_connected():
            return None, None
        acc = RemoteServerAccess(conn, sid)
        # A fresh access object assumes "stopped"; without the Machine's last
        # report, Stop Selected on a running remote Server silently did nothing.
        acc.note_status(self._remote_server_running(mid, sid))
        return acc, self._server_display_name(mid, f"r|{mid}|{sid}")

    def _announce_servers_changed(self):
        """A Server was added, removed or renamed here: tell connected Administrators
        now, instead of at the next start/stop."""
        host = getattr(self, "remote_host", None)
        if host is not None and host.is_running():
            try:
                host.notify_servers_changed()
            except Exception:
                _LOG.exception("Couldn't tell Administrators about the changed Server list")

    def _server_display_name(self, machine_id: Optional[str], key: str) -> str:
        """'Server N: <Active World>' for confirmations, as on the top line."""
        e = next((e for e in self._machine_server_entries(machine_id) if e["key"] == key), None)
        return f"Server {e['number']}: {e['world']}" if e else "Server"

    def _fleet_row_double_click(self, event=None):
        tree = self._fleet_tree
        sel = tree.selection()
        if not sel:
            return
        info = self._fleet_rows.get(sel[0])
        if not info:
            return
        if info[0] == "local":
            self.sidebar_tree.selection_set(f"profile:{info[1]}")
        else:
            self.sidebar_tree.selection_set(f"rprofile:{info[1]}:{info[2]}")

    def _fleet_selected_access(self):
        sel = self._fleet_tree.selection()
        if not sel:
            messagebox.showwarning("Fleet", "Select a Server first.")
            return None, None
        acc, name = self._fleet_access_for_row(sel[0])
        if acc is None:
            messagebox.showwarning("Fleet", "That Server isn't reachable right now.")
        return acc, name

    def fleet_start_selected(self):
        acc, name = self._fleet_selected_access()
        if acc is None or acc.is_running():
            return
        if not messagebox.askyesno("Start Server", f"Start '{name}'?"):
            return
        def do(a=acc):
            try:
                a.start()
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("Fleet", str(e)))
            self.root.after(800, self.refresh_sidebar)
        threading.Thread(target=do, daemon=True).start()

    def fleet_stop_selected(self):
        acc, name = self._fleet_selected_access()
        if acc is None or not acc.is_running():
            return
        if not messagebox.askyesno("Stop Server", f"Stop '{name}'?"):
            return
        def do(a=acc):
            try:
                a.stop()
            except Exception as e:
                self.root.after(0, lambda e=e: messagebox.showerror("Fleet", str(e)))
            self.root.after(800, self.refresh_sidebar)
        threading.Thread(target=do, daemon=True).start()

    def remove_machine_confirm(self, machine_id: str):
        machine = next((m for m in self.config.get("machines", []) if m["id"] == machine_id), None)
        name = (machine or {}).get("name", machine_id)
        if not messagebox.askyesno("Remove Machine",
                f"Remove '{name}'?\n\nThis only removes it from this administrator -- "
                "the Machine itself and its Servers are unaffected."):
            return
        if self.active_remote and self.active_remote[0] == machine_id:
            self.active_access = None
            self.active_remote = None
        self._stop_connection(machine_id)
        self.remove_machine(machine_id)
        self._overview_kind = ("fleet",)
        self.build_fleet_overview()
        self.refresh_sidebar()

    def setup_main_tab(self):
        if self.layout == "menu":   # one row per choice (mockup 2)
            self._setup_main_tab_menu()
            return
        self.main_tab.columnconfigure(0, weight=1)
        
        header = ttk.Frame(self.main_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Update Bedrock Server", font=self.font(14, "bold")).pack(side=tk.LEFT)

        # The app asks Mojang for the newest release itself (fetch_latest_bedrock_release),
        # so updating is one button -- no Wiki lookup, typing or ZIP hunting.
        version_frame = ttk.LabelFrame(self.main_tab, text="Bedrock Server Version", padding=10)
        version_frame.grid(row=1, column=0, sticky="ew", pady=5)
        version_frame.columnconfigure(0, weight=1)
        self.update_version_label = ttk.Label(version_frame, text="Installed: —   ·   Latest: —",
                                              font=self.font(11))
        self.update_version_label.grid(row=0, column=0, sticky="w")
        self.update_state_label = ttk.Label(version_frame, text="", foreground=self.fg("gray"))
        self.update_state_label.grid(row=1, column=0, sticky="w", pady=(2, 6))
        actions = ttk.Frame(version_frame)
        actions.grid(row=2, column=0, sticky="w")
        self.update_button = self.button(actions, text="Update", command=self.start_update, style="Primary.TButton")
        self.update_button.pack(side=tk.LEFT)
        # Only shown with two or more Servers on this Machine (_refresh_update_button).
        self.update_all_button = self.button(actions, text="Update all Servers", command=self.update_all_servers)
        self.button(actions, text="🔄 Check again", style="Small.TButton",
                   command=lambda: self.refresh_update_status(force=True)).pack(side=tk.LEFT, padx=(8, 0))
        self.advanced_toggle = self.button(actions, text="Advanced ▸", style="Small.TButton",
                                          command=self._toggle_update_advanced)
        self.advanced_toggle.pack(side=tk.LEFT, padx=(8, 0))

        # Advanced: a specific ZIP or a manually entered version. Only needed if
        # the automatic check can't reach Mojang (it opens by itself then).
        self._build_update_advanced(version_frame)

        # Per-Server update policy (docs/V2-MAJORDOMO-PLAN.md) -- lives here,
        # not the app-level Settings tab, since it's specific to this Server.
        update_settings_frame = ttk.LabelFrame(self.main_tab, text="Update Settings (this Server)", padding=10)
        update_settings_frame.grid(row=2, column=0, sticky="ew", pady=5)
        self.auto_stop_var = tk.BooleanVar(value=self.config.get("auto_stop_server_before_update", True))
        self.check(update_settings_frame, text="Automatically stop server before update", variable=self.auto_stop_var).grid(row=0, column=0, sticky="w", pady=2)
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start_server_after_update", False))
        self.check(update_settings_frame, text="Automatically start server after update", variable=self.auto_start_var).grid(row=1, column=0, sticky="w", pady=2)
        self.button(update_settings_frame, text="💾 Save", command=self.save_settings).grid(row=2, column=0, sticky="w", pady=(6, 0))

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

        # Secondary, occasional actions: smaller and tucked to the right.
        extras = ttk.Frame(button_frame)
        extras.pack(side=tk.RIGHT)
        self.button(extras, text="📂 Open Folder", command=self.open_server_folder, style="Small.TButton").pack(side=tk.RIGHT, padx=2)
        self.button(extras, text="📋 Dry Run", command=self.dry_run, style="Small.TButton").pack(side=tk.RIGHT, padx=2)
        
        log_frame = ttk.LabelFrame(self.main_tab, text="Activity Log", padding=5)
        log_frame.grid(row=6, column=0, sticky="nsew", pady=5)
        self.main_tab.rowconfigure(6, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self._build_activity_log(log_frame)

    def _build_update_advanced(self, parent):
        """Update ▸ Advanced (hidden until opened): a specific ZIP or a typed version.
        Only needed if the automatic check can't reach Mojang (it opens by itself then)."""
        self.advanced_frame = ttk.Frame(parent)
        self.advanced_frame.columnconfigure(1, weight=1)
        ttk.Label(self.advanced_frame, text="Bedrock Server ZIP:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.zip_entry = ttk.Entry(self.advanced_frame)
        self.zip_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.zip_entry.bind("<KeyRelease>", self.validate_inputs)
        self.button(self.advanced_frame, text="Browse", command=self.browse_zip).grid(row=0, column=2, padx=5)
        self.button(self.advanced_frame, text="Enter a version…", command=self.manual_version_input).grid(row=0, column=3, padx=5)
        self.zip_status = ttk.Label(self.advanced_frame, text="", foreground=self.fg("gray"))
        self.zip_status.grid(row=1, column=1, sticky="w", padx=5)

    def _build_activity_log(self, log_frame):
        log_scroll = ttk.Scrollbar(log_frame)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED,
                               font=("Consolas" if sys.platform == "win32" else "Monaco", self.config.get("console_font_size", 9)))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.config(command=self.log_text.yview)
        self.log_text.config(yscrollcommand=log_scroll.set)
        self.log_text.tag_config("info", foreground=self.fg("#2196F3"))
        self.log_text.tag_config("success", foreground=self.fg("#4CAF50"))
        self.log_text.tag_config("warning", foreground=self.fg("#FF9800"))
        self.log_text.tag_config("error", foreground=self.fg("#F44336"))
        self.log("Application started. (First time? Press + at the top right to create your first Server.)", "info")
    
    def setup_server_tab(self):
        if self.layout == "menu":   # rows + a chat-style console (Teleport has its own page)
            self._setup_server_tab_menu()
            return
        self.server_tab.columnconfigure(0, weight=1)
        self.server_tab.rowconfigure(2, weight=1)
        info_frame = ttk.LabelFrame(self.server_tab, text="Active Server Information", padding=10)
        info_frame.grid(row=0, column=0, sticky="ew", pady=5)
        self.info_text = ttk.Label(info_frame, text="No Server yet — press + at the top right to create one.")
        self.info_text.pack(anchor="w")
        control_frame = ttk.LabelFrame(self.server_tab, text="Server Control", padding=10)
        control_frame.grid(row=1, column=0, sticky="ew", pady=5)
        btn_frame = ttk.Frame(control_frame)
        btn_frame.pack(fill=tk.X)
        self.start_btn = self.button(btn_frame, text="▶️ Start", command=self.start_server, width=15,
                                     style="Primary.TButton" if self.look == "crafty" else "")
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.stop_btn = self.button(btn_frame, text="⏹️ Stop", command=self.stop_server, width=15)
        self.stop_btn.pack(side=tk.LEFT, padx=5)
        self.restart_btn = self.button(btn_frame, text="🔄 Restart", command=self.restart_server, width=15)
        self.restart_btn.pack(side=tk.LEFT, padx=5)
        self.button(btn_frame, text="🎲 Gamerules", command=self.open_gamerules_dialog, width=15).pack(side=tk.LEFT, padx=5)
        self.server_running_label = ttk.Label(btn_frame, text="⬤ Stopped", foreground=self.fg("red"))
        self.server_running_label.pack(side=tk.RIGHT, padx=20)
        world_frame = ttk.Frame(control_frame)
        world_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(world_frame, text="Active World:").pack(side=tk.LEFT)
        self.world_combo = ttk.Combobox(world_frame, state="readonly", width=32)
        self.world_combo.pack(side=tk.LEFT, padx=8)
        self.world_combo.bind("<<ComboboxSelected>>", self.on_world_selected)
        # Empty while stopped; explains the greyed-out dropdown while the Server runs.
        self.world_hint_label = ttk.Label(world_frame, text="", font=self.font(8), foreground=self.fg("#FF9800"))
        self.world_hint_label.pack(side=tk.LEFT)
        net_frame = ttk.Frame(control_frame)
        net_frame.pack(fill=tk.X, pady=(10, 0))
        self.network_label = ttk.Label(net_frame, text="Network: Not configured")
        self.network_label.pack(side=tk.LEFT)
        self.button(net_frame, text="📋 Copy IP", command=self.copy_server_ip).pack(side=tk.RIGHT)
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
        self.button(cmd_frame, text="Send", command=self.send_server_command).grid(row=0, column=1)
        quick_frame = ttk.Frame(console_frame)
        quick_frame.grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(quick_frame, text="Quick:").pack(side=tk.LEFT, padx=(0, 5))
        for cmd in ["list", "save hold", "save query", "save resume", "say Hello!"]:
            self.button(quick_frame, text=cmd, command=lambda c=cmd: self.quick_command(c)).pack(side=tk.LEFT, padx=2)
        # Teleport one player to another (console `tp`: works with cheats off).
        tp_frame = ttk.Frame(console_frame)
        tp_frame.grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(tp_frame, text="📍 Teleport").pack(side=tk.LEFT, padx=(0, 5))
        self.tp_from = ttk.Combobox(tp_frame, width=18)
        self.tp_from.configure(postcommand=lambda: self.tp_from.configure(values=self._player_choices()))
        self.tp_from.pack(side=tk.LEFT)
        ttk.Label(tp_frame, text="to").pack(side=tk.LEFT, padx=5)
        self.tp_to = ttk.Combobox(tp_frame, width=18)
        self.tp_to.configure(postcommand=lambda: self.tp_to.configure(values=self._player_choices()))
        self.tp_to.pack(side=tk.LEFT)
        self.button(tp_frame, text="Go", command=self.teleport_player).pack(side=tk.LEFT, padx=(6, 0))
        self.tp_feedback = ttk.Label(tp_frame, text="", font=self.font(10, "bold"))
        self.tp_feedback.pack(side=tk.LEFT, padx=(10, 0))
        self._reply_slot("teleport", self.tp_feedback)
    
    def setup_backup_tab(self):
        if self.layout == "menu":   # one row per choice (mockup 2)
            self._setup_backup_tab_menu()
            return
        self.backup_tab.columnconfigure(0, weight=1)
        self.backup_tab.rowconfigure(4, weight=1)
        self.backup_header_label = ttk.Label(self.backup_tab, text="Backups for: (no Server selected)",
                                             font=self.font(10, "bold"))
        self.backup_header_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        control_frame = ttk.Frame(self.backup_tab)
        control_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.button(control_frame, text="💾 Create Backup Now", command=self.manual_backup).pack(side=tk.LEFT, padx=5)
        self.button(control_frame, text="🧹 Cleanup Old Backups", command=self.cleanup_backups).pack(side=tk.LEFT, padx=5)
        self.button(control_frame, text="📂 Open Folder", command=self.open_backup_folder).pack(side=tk.LEFT, padx=5)
        self.button(control_frame, text="🔄 Refresh", command=self.refresh_backups).pack(side=tk.RIGHT, padx=5)
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
            cb = self.check(self.preserve_inner, text=text, variable=var)
            cb.grid(row=row, column=col, sticky="w", padx=10, pady=2)
            self.create_tooltip(cb, props["description"])
            col += 1
            if col >= 3:
                col = 0
                row += 1
        # Backup policy is per Server (docs/V2-MAJORDOMO-PLAN.md), so it lives
        # here beside the preserve list rather than in the app-level Settings tab.
        backup_settings_frame = ttk.LabelFrame(self.backup_tab, text="Backup Settings (this Server)", padding=10)
        backup_settings_frame.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Label(backup_settings_frame, text="Maximum backups to keep:").grid(row=0, column=0, sticky="w", pady=5)
        self.max_backups_var = tk.IntVar(value=self.config.get("max_backups", 5))
        ttk.Spinbox(backup_settings_frame, from_=1, to=50, width=10, textvariable=self.max_backups_var).grid(row=0, column=1, sticky="w", padx=10)
        self.auto_cleanup_var = tk.BooleanVar(value=self.config.get("auto_cleanup_backups", True))
        self.check(backup_settings_frame, text="Automatically cleanup old backups after update", variable=self.auto_cleanup_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.compress_var = tk.BooleanVar(value=self.config.get("compress_backups", False))
        self.check(backup_settings_frame, text="Compress backups (ZIP format, slower but smaller)", variable=self.compress_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        self.button(backup_settings_frame, text="💾 Save", command=self.save_settings).grid(row=3, column=0, sticky="w", pady=(6, 0))
        list_frame = ttk.LabelFrame(self.backup_tab, text="Available Backups", padding=10)
        list_frame.grid(row=4, column=0, sticky="nsew")
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
        action_frame.grid(row=5, column=0, sticky="ew", pady=10)
        self.button(action_frame, text="🔄 Restore Selected", command=self.restore_selected_backup).pack(side=tk.LEFT, padx=5)
        self.button(action_frame, text="❌ Delete Selected", command=self.delete_selected_backup).pack(side=tk.LEFT, padx=5)
        self.button(action_frame, text="📂 Open in Explorer", command=self.open_selected_backup).pack(side=tk.LEFT, padx=5)
    
    def setup_worlds_tab(self):
        if self.layout == "menu":   # one row per choice (mockup 2)
            self._setup_worlds_tab_menu()
            return
        self.worlds_tab.columnconfigure(0, weight=1)
        self.worlds_tab.rowconfigure(1, weight=1)
        control_frame = ttk.Frame(self.worlds_tab)
        control_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.button(control_frame, text="✨ Create New World", command=self.create_new_world,
                   style="Primary.TButton").pack(side=tk.LEFT, padx=5)
        self.button(control_frame, text="🔄 Refresh", command=self.refresh_worlds).pack(side=tk.RIGHT, padx=5)
        self.button(control_frame, text="📂 Open Worlds Folder", command=self.open_worlds_folder).pack(side=tk.RIGHT, padx=5)
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
        self.button(action_frame, text="🎯 Set as Active World", command=self.set_selected_world_active).pack(side=tk.LEFT, padx=5)
        self.button(action_frame, text="✏️ Rename", command=self.rename_selected_world).pack(side=tk.LEFT, padx=5)
        self.button(action_frame, text="🗑️ Delete", command=self.delete_selected_world).pack(side=tk.LEFT, padx=5)
        self.world_info_label = ttk.Label(
            self.worlds_tab,
            text="A World's save is upgraded by the version that runs it — it won't load on an older Bedrock Server Version.",
            font=self.font(8), foreground=self.fg("gray"))
        self.world_info_label.grid(row=3, column=0, sticky="w")
    
    def refresh_world_combo(self):
        """Fill the Active World dropdown from the Server (local or remote).

        list_worlds() walks every world folder on disk (get_folder_size) --
        can take seconds against a large, actively-played world with the real
        engine writing to it, so this must not block the main thread (same
        root cause as update_server_info/refresh_worlds)."""
        if not hasattr(self, 'world_combo'):
            return
        acc = self.active_access
        if acc is None:
            self.world_combo.config(values=[])
            self.world_combo.set("")
            return

        def worker(acc=acc):
            try:
                worlds = [w["name"] for w in acc.list_worlds()]
                current = acc.get_active_world()
            except Exception:
                self.root.after(0, lambda: self._apply_world_combo(acc, None, None))
                return
            self.root.after(0, lambda: self._apply_world_combo(acc, worlds, current))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_world_combo(self, acc, worlds, current):
        if self.active_access is not acc:
            return  # Server was switched while this fetch was in flight -- discard.
        if worlds is None:
            self.world_combo.config(values=[])
            self.world_combo.set("")
            return
        if current and current not in worlds:
            worlds = worlds + [current]  # created but not generated yet
        # Plain world names; the dropdown's selected value IS the active one
        # (the "Active World:" label already says what it is).
        self.world_combo.config(values=worlds)
        self.world_combo.set(current)

    def set_active_world(self, new_name: str) -> bool:
        """Point level-name at a world folder; takes effect on next Server start."""
        acc = self.active_access
        if acc is None or not new_name:
            return False
        try:
            ok = acc.set_active_world(new_name)
        except Exception as e:
            messagebox.showerror("Error", f"Could not update the Active World:\n{e}")
            return False
        if ok:
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
        acc = self.active_access
        if acc and acc.is_running():
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
        acc = self.active_access
        if acc is None:
            return
        try:
            if name == acc.get_active_world():
                self.log(f"'{name}' is already the Active World", "info")
                return
        except Exception:
            pass
        if acc.is_running():
            # Explicit switch action: confirm, then stop nicely -> switch -> start again.
            if not messagebox.askyesno("Switch World",
                    f"The Server is running.\n\nStop it nicely, switch to '{name}', and start it again?"):
                return
            self.log(f"Switching to '{name}': stopping the running Server...", "info")
            def do_switch(a=acc):
                a.stop()
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
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Warning", "No Server selected.")
            return
        if acc.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before creating a new World.")
            return
        asked = self._ask_new_world()
        if not asked:
            return
        name, seed = asked
        if not name or any(c in name for c in '/\\:*?"<>|'):
            messagebox.showerror("Invalid Name", "The World name is empty or contains invalid characters.")
            return
        try:
            existing = [w["name"] for w in acc.list_worlds()]
        except Exception:
            existing = []
        if name in existing:
            if not messagebox.askyesno("World Exists", f"'{name}' already exists.\n\nSet it as the Active World instead?"):
                return
        if self.set_active_world(name):
            self.refresh_world_combo()
            self.refresh_worlds()
            if name not in existing:
                # level-seed is read only when Bedrock generates a World, and stays in
                # server.properties afterwards -- so it's always written, emptied when
                # no seed was given, or the next new World would silently reuse it.
                try:
                    acc.write_properties({"level-seed": seed})
                except Exception as e:
                    messagebox.showerror("Error", f"Could not save the seed to server.properties:\n{e}")
                # Soft link: offer to name the Server after the World, but only while it
                # still carries the stock name (never overwrite a name the user chose).
                props = acc.read_properties()
                if props.get("server-name", "").strip() in ("", "Dedicated Server"):
                    if messagebox.askyesno("Name the Server too?",
                            "Your Server still has the stock name 'Dedicated Server' — that's the name\n"
                            "players see in their server list when they connect.\n\n"
                            f"Name the Server '{name}' as well?"):
                        props["server-name"] = name
                        acc.write_properties(props)
                        self.update_server_info()
                seed_note = f"Seed: {seed}\n" if seed else "Seed: random\n"
                messagebox.showinfo("World Created",
                    f"'{name}' is now the Active World.\n{seed_note}\n"
                    "Bedrock will generate it the first time you start the Server.\n"
                    "Taking you to 📝 Configuration — gamemode and difficulty\n"
                    "shape the new World on its first start.")
                self.notebook.select(self.properties_editor)
                self.properties_editor.load_properties()

    def _ask_new_world(self) -> Optional[Tuple[str, str]]:
        """Name + optional seed for a new World; (name, seed) or None if cancelled."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Create New World")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=14)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="Name of the new World:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=3)
        name_var, seed_var = tk.StringVar(), tk.StringVar()
        name_entry = ttk.Entry(body, textvariable=name_var, width=30)
        name_entry.grid(row=0, column=1, sticky="w", pady=3)
        ttk.Label(body, text="Seed (optional):").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        seed_entry = ttk.Entry(body, textvariable=seed_var, width=30)
        seed_entry.grid(row=1, column=1, sticky="w", pady=3)
        self.create_tooltip(seed_entry, "The same seed gives the same landscape.\nLeave it empty for a random World.")
        result = []
        def ok(_=None):
            if name_var.get().strip():
                result.append((name_var.get().strip(), seed_var.get().strip()))
                dlg.destroy()
        btns = ttk.Frame(body); btns.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))
        self.button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        self.button(btns, text="Create", style="Primary.TButton", command=ok).pack(side=tk.RIGHT, padx=(0, 6))
        dlg.bind("<Return>", ok)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        name_entry.focus_set()
        dlg.grab_set()
        self.root.wait_window(dlg)
        return result[0] if result else None

    def rename_selected_world(self):
        acc = self.active_access
        if acc is None:
            return
        selected = self.world_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No World selected.")
            return
        old_name = str(self.world_tree.item(selected[0])["values"][0])
        if acc.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before renaming a World.")
            return
        new_name = simpledialog.askstring("Rename World", f"New name for '{old_name}':", parent=self.root)
        if not new_name:
            return
        new_name = new_name.strip()
        if not new_name or any(c in new_name for c in '/\\:*?"<>|'):
            messagebox.showerror("Invalid Name", "The World name is empty or contains invalid characters.")
            return
        try:
            existing = {w["name"] for w in acc.list_worlds()}
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        if old_name not in existing:
            messagebox.showinfo("Not created yet",
                f"'{old_name}' hasn't been generated yet — it's only the Active World pointer.\n"
                "Start the Server once to create it, or just create a new World with the name you want.")
            return
        if new_name in existing:
            messagebox.showerror("Exists", f"A World named '{new_name}' already exists.")
            return
        try:
            ok = acc.rename_world(old_name, new_name)
        except Exception as e:
            messagebox.showerror("Error", f"Could not rename World:\n{e}")
            return
        if not ok:
            messagebox.showerror("Error", "Could not rename World.")
            return
        self.log(f"Renamed World '{old_name}' to '{new_name}'", "success")
        self.refresh_worlds()
        self.refresh_world_combo()

    def delete_selected_world(self):
        acc = self.active_access
        if acc is None:
            return
        selected = self.world_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No World selected.")
            return
        name = str(self.world_tree.item(selected[0])["values"][0])
        try:
            active = acc.get_active_world()
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        if name == active:
            messagebox.showwarning("Active World", "You can't delete the Active World. Switch to another World first.")
            return
        if acc.is_running():
            messagebox.showwarning("Server Running", "Stop the Server before deleting a World.")
            return
        if not messagebox.askyesno("Delete World",
                f"Permanently delete the World '{name}'?\n\nThis cannot be undone (older backups may still contain it)."):
            return
        try:
            if acc.delete_world(name):
                self.log(f"Deleted World: {name}", "info")
            else:
                messagebox.showerror("Error", "Could not delete World.")
        except Exception as e:
            messagebox.showerror("Error", f"Could not delete World:\n{e}")
        self.refresh_worlds()
        self.refresh_world_combo()
        self.update_server_info()

    def setup_players_tab(self):
        if self.layout == "menu":   # one row per choice (mockup 2)
            self._setup_players_tab_menu()
            return
        self.players_tab.columnconfigure(0, weight=1)
        # --- Access: the allowlist (Bedrock's only join control — there is no blacklist) ---
        access = ttk.LabelFrame(self.players_tab, text="Access — who may join (allowlist.json)", padding=10)
        access.grid(row=0, column=0, sticky="ew", pady=5)
        access.columnconfigure(0, weight=1)
        self.allowlist_var = tk.BooleanVar(value=False)
        self.check(access,
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
        self.button(abtns, text="➕ Add", command=self.add_allowlist_player, width=10).pack(pady=2)
        self.button(abtns, text="➖ Remove", command=self.remove_allowlist_player, width=10).pack(pady=2)
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
        self.button(perms, text="Operator", command=lambda: self.set_player_permission("operator")).grid(row=1, column=2, padx=2, pady=(6, 0))
        self.button(perms, text="Member", command=lambda: self.set_player_permission("member")).grid(row=1, column=3, padx=2, pady=(6, 0))
        self.button(perms, text="Visitor", command=lambda: self.set_player_permission("visitor")).grid(row=1, column=4, padx=2, pady=(6, 0))
        self.button(perms, text="Remove entry", command=self.remove_permission_entry).grid(row=1, column=5, padx=(10, 0), pady=(6, 0))
        # --- Per-player game mode: mixed survival/creative on one Server ---
        gm = ttk.LabelFrame(self.players_tab, text="Game mode — per player (mix survival and creative on one Server)", padding=10)
        gm.grid(row=2, column=0, sticky="ew", pady=5)
        ttk.Label(gm, text="Player:").grid(row=0, column=0, sticky="e")
        self.gm_player_combo = ttk.Combobox(gm, width=22)
        self.gm_player_combo.grid(row=0, column=1, sticky="w", padx=4)
        self.button(gm, text="⛏️ Survival", command=lambda: self.set_player_gamemode("survival")).grid(row=0, column=2, padx=2)
        self.button(gm, text="🧱 Creative", command=lambda: self.set_player_gamemode("creative")).grid(row=0, column=3, padx=2)
        self.button(gm, text="🗺️ Adventure", command=lambda: self.set_player_gamemode("adventure")).grid(row=0, column=4, padx=2)
        # The Server's own answer to the last button press (_check_console_replies):
        # the console that shows it lives on another tab.
        self.gm_feedback = ttk.Label(gm, text="", font=self.font(10, "bold"))
        self.gm_feedback.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(gm, text="Applies live to an online player and sticks for them afterwards (Server must be running).",
                  font=self.font(8), foreground=self.fg("gray")).grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.gm_force_warn = ttk.Label(gm, text="", font=self.font(8), foreground=self.fg("#F44336"))
        self.gm_force_warn.grid(row=3, column=0, columnspan=5, sticky="w")
        self._reply_slot("gamemode", self.gm_feedback)
        foot = ttk.Frame(self.players_tab)
        foot.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.button(foot, text="🔍 Scan console for players", command=self.scan_console_for_players).pack(side=tk.LEFT)
        ttk.Label(foot,
                  text="Names + XUIDs are learned from join lines automatically; the scan re-reads the whole console if one was missed.",
                  font=self.font(8), foreground=self.fg("gray")).pack(side=tk.LEFT, padx=8)

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
        if not hasattr(self, 'allow_tree'):
            return
        acc = self.active_access
        for i in self.allow_tree.get_children():
            self.allow_tree.delete(i)
        for i in self.perm_tree.get_children():
            self.perm_tree.delete(i)
        self._clear_stale_replies()
        if acc is None:
            self.perm_player_combo.config(values=[])
            self.gm_player_combo.config(values=[])
            self.gm_force_warn.config(text="")
            self._sync_menu_rows("players")
            return
        try:
            data = acc.get_players()
        except Exception:
            return
        self.allowlist_var.set(bool(data.get("allow_list_enabled")))
        allow_entries = data.get("allowlist", [])
        for e in allow_entries:
            self.allow_tree.insert("", tk.END, values=(e.get("name", ""), e.get("xuid", ""),
                                                       "yes" if e.get("ignoresPlayerLimit") else "no"))
        known = data.get("known_players", {})
        by_xuid = {str(v): k for k, v in known.items()}
        for e in data.get("permissions", []):
            x = str(e.get("xuid", ""))
            self.perm_tree.insert("", tk.END, values=(by_xuid.get(x, "(unknown)"), x, e.get("permission", "member")))
        names = sorted({n for n in list(known.keys()) + [a.get("name", "") for a in allow_entries] if n}, key=str.lower)
        self.perm_player_combo.config(values=names)
        self.gm_player_combo.config(values=names)
        fg = bool(data.get("force_gamemode"))
        self.gm_force_warn.config(text=("⚠ force-gamemode=true resets everyone to the default mode on every join — "
                                        "set it to false in 📝 Configuration to allow mixed modes." if fg else ""))
        self._sync_menu_rows("players")

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
        text = "\n".join(self._console_lines) if self.layout == "menu" else self.console_text.get("1.0", tk.END)
        found = self._learn_players_from_text(text)
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
        acc = self.active_access
        if acc is None:
            self.allowlist_var.set(False)
            messagebox.showwarning("Players", "No Server selected.")
            return
        enable = self.allowlist_var.get()
        try:
            if enable and not acc.get_players().get("allowlist"):
                if not messagebox.askyesno("Empty allowlist",
                        "The allowlist is empty — with enforcement ON, nobody can join until you add players.\n\nTurn it on anyway?"):
                    self.allowlist_var.set(False)
                    return
            acc.set_allowlist_enforcement(enable)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            self.refresh_players_tab()
            return
        self.log(f"Allowlist enforcement {'ON' if enable else 'OFF'}", "success")

    def add_allowlist_player(self):
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Players", "No Server selected.")
            return
        name = simpledialog.askstring("Add player", "Player gamertag (exact Xbox name):", parent=self.root)
        if not name or not name.strip():
            return
        name = name.strip()
        try:
            if any(e.get("name", "").lower() == name.lower() for e in acc.get_players().get("allowlist", [])):
                messagebox.showinfo("Already listed", f"'{name}' is already on the allowlist.")
                return
            acc.add_allowlist_player(name)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        self.log(f"Added '{name}' to the allowlist", "success")
        self.refresh_players_tab()

    def remove_allowlist_player(self):
        sel = self.allow_tree.selection()
        if not sel:
            messagebox.showwarning("Players", "Select an allowlist entry to remove.")
            return
        acc = self.active_access
        if acc is None:
            return
        name = str(self.allow_tree.item(sel[0])["values"][0])
        try:
            acc.remove_allowlist_player(name)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        self.log(f"Removed '{name}' from the allowlist", "info")
        self.refresh_players_tab()

    def set_player_permission(self, level: str):
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Players", "No Server selected.")
            return
        name = self.perm_player_combo.get().strip()
        if not name:
            messagebox.showwarning("Players", "Pick or type a player name first.")
            return
        try:
            known = acc.get_players().get("known_players", {})
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        xuid = known.get(name)
        if not xuid:
            xuid = simpledialog.askstring("XUID needed",
                f"No XUID known for '{name}' yet — Bedrock keys roles by XUID.\n"
                "Easiest: Cancel, and have them join once while the Server runs.\n"
                "Or enter their XUID manually:", parent=self.root)
            if not xuid:
                return
            xuid = xuid.strip()
            if not xuid.isdigit():
                messagebox.showerror("Players", "A XUID is a number.")
                return
            if not self.active_remote:  # remember it locally for convenience
                self.config.setdefault("known_players", {})[name] = xuid
                save_config(self.config)
        try:
            acc.set_permission(str(xuid), level)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        self.log(f"Role for '{name}' set to {level}", "success")
        self.refresh_players_tab()

    def remove_permission_entry(self):
        sel = self.perm_tree.selection()
        if not sel:
            messagebox.showwarning("Players", "Select a role entry to remove.")
            return
        acc = self.active_access
        if acc is None:
            return
        xuid = str(self.perm_tree.item(sel[0])["values"][1])
        try:
            acc.remove_permission(xuid)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        self.log("Removed role entry (player is back at the default level)", "info")
        self.refresh_players_tab()

    def set_player_gamemode(self, mode: str):
        acc = self.active_access
        if acc is None or not acc.is_running():
            messagebox.showwarning("Server stopped",
                "Start the Server first — game mode is set live, per player, while they're online.")
            return
        name = self.gm_player_combo.get().strip()
        if not name:
            messagebox.showwarning("Players", "Pick or type a player name first.")
            return
        self._end_reply_wait("gamemode")
        try:
            acc.set_gamemode(name, mode)
        except Exception as e:
            messagebox.showerror("Players", str(e))
            return
        self.console_log(f'> gamemode {mode} "{name}"')

        def interpret(low, line, name=name, mode=mode.capitalize()):
            # Bedrock: 'No targets matched selector' when the player isn't online,
            # otherwise a line naming the player and their new game mode.
            if "no targets matched" in low:
                return f"✗ {name} isn't online, so their game mode can't be changed right now.", False
            if name.lower() in low and "mode" in low:
                return f"✓ {name} is now in {mode}.", True
            return None
        self._await_reply("gamemode", acc, f"⏳ Asking the Server to put {name} in {mode.capitalize()}…",
                          interpret, f"No answer from the Server about {name} — see the console on 🎮 Server.")

    def teleport_player(self):
        """Server tab: move one player to another with the console's `tp`. Console
        commands work whether or not allow-cheats is on (and need no Operator)."""
        self._do_teleport(self.tp_from.get().strip(), self.tp_to.get().strip())

    def _do_teleport(self, a: str, b: str):
        """Send `tp "a" "b"` and show the Server's answer by the button that asked
        (the Server tab's row in the tab layout, the Teleport page in the New layout)."""
        acc = self.active_access
        self._end_reply_wait("teleport")
        if acc is None or not acc.is_running():
            self._show_reply("teleport", "Start the Server first — players can only be moved while they're playing.",
                             "#E65100")
            return
        if not a or not b:
            self._show_reply("teleport", "Pick who to move, and who to move them to.", "#E65100")
            return
        if a.lower() == b.lower():
            self._show_reply("teleport", "Pick two different players.", "#E65100")
            return
        cmd = f'tp "{a}" "{b}"'
        try:
            acc.send_command(cmd)
        except Exception as e:
            self._show_reply("teleport", f"✗ Couldn't send it: {e}", "#C62828")
            return
        self.console_log(f"> {cmd}")

        def interpret(low, line, a=a, b=b):
            if "no targets matched" in low:
                return f"✗ {a} or {b} isn't online, so nobody was moved.", False
            if "teleport" in low and a.lower() in low:
                return f"✓ {a} was teleported to {b}.", True
            return None
        self._await_reply("teleport", acc, f"⏳ Moving {a} to {b}…", interpret,
                          "No answer from the Server — see the console on the Server page."
                          if self.layout == "menu" else "No answer from the Server — see the console above.")

    def _player_choices(self) -> List[str]:
        """Names for the teleport pickers: who's online when this Server knows it
        (a local Server, from join/leave lines), otherwise every known player."""
        acc = self.active_access
        try:
            online = acc.online_players() if acc else None
        except Exception:
            online = None
        return list(online) if online else list(self.gm_player_combo.cget("values"))

    # --- The Server's console answer, shown next to the button that asked ---
    # Used by the game-mode buttons (Players) and Teleport (Server): the reply
    # otherwise only reaches the console, which may be on another tab -- and
    # Bedrock's wording ("No targets matched selector") means little to a kid.
    def _reply_slot(self, kind: str, label=None) -> dict:
        slots = self.__dict__.setdefault("_replies", {})
        if kind not in slots:
            slots[kind] = {"label": label, "access": None, "wait": None, "fade": None}
        elif label is not None:
            slots[kind]["label"] = label
        return slots[kind]

    def _await_reply(self, kind: str, access, waiting_text: str, interpret, timeout_text: str):
        slot = self._reply_slot(kind)
        self._end_reply_wait(kind)
        self._show_reply(kind, waiting_text, "gray", fade=False)
        slot["wait"] = {"access": access, "interpret": interpret, "timeout_text": timeout_text,
                        "since": time.monotonic(),
                        "timer": self.root.after(CONSOLE_REPLY_TIMEOUT_MS, lambda: self._reply_timeout(kind))}

    def _check_console_replies(self, line: str):
        """Called with every console line of the selected Server (local or remote)."""
        low = line.lower()
        waiting = sorted((s["wait"]["since"], k) for k, s in self.__dict__.get("_replies", {}).items() if s["wait"])
        for _, kind in waiting:  # oldest question first: replies come back in order
            wait = self._replies[kind]["wait"]
            if self.active_access is not wait["access"]:
                continue
            result = wait["interpret"](low, line)
            if result is None and ("syntax error" in low or "unknown command" in low):
                result = ("✗ The Server said: " + re.sub(r"^\[[^\]]*\]\s*", "", line).strip(), False)
            if result is None:
                continue  # some other console line; keep waiting
            text, ok = result
            self._end_reply_wait(kind)
            self._show_reply(kind, text, "#2E7D32" if ok else "#C62828")
            self.log(text.lstrip("✓✗ "), "success" if ok else "warning")
            return

    def _reply_timeout(self, kind: str):
        slot = self._reply_slot(kind)
        wait, slot["wait"] = slot["wait"], None
        if wait and self.active_access is wait["access"]:
            self._show_reply(kind, wait["timeout_text"], "#E65100")

    def _end_reply_wait(self, kind: str):
        slot = self._reply_slot(kind)
        wait, slot["wait"] = slot["wait"], None
        if wait:
            try:
                self.root.after_cancel(wait["timer"])
            except tk.TclError:
                pass

    def _show_reply(self, kind: str, text: str, colour: str, fade: bool = True):
        """Show text in that button's answer line; a final answer fades after a
        while so an old result isn't mistaken for a new one."""
        slot = self._reply_slot(kind)
        if slot["fade"]:
            self.root.after_cancel(slot["fade"])
            slot["fade"] = None
        if slot["label"] is not None:
            slot["label"].config(text=text, foreground=self.fg(colour))
        slot["access"] = self.active_access
        if fade and text:
            slot["fade"] = self.root.after(CONSOLE_REPLY_SHOW_MS, lambda: self._show_reply(kind, "", "gray", fade=False))

    def _clear_stale_replies(self):
        """After switching Server: an answer about another Server doesn't belong here."""
        for kind, slot in self.__dict__.get("_replies", {}).items():
            if slot["access"] is not self.active_access:
                self._end_reply_wait(kind)
                self._show_reply(kind, "", "gray", fade=False)

    def open_gamerules_dialog(self):
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Gamerules", "No Server selected.")
            return
        try:
            active = acc.get_active_world()
            current = acc.read_gamerules()
        except Exception as e:
            messagebox.showerror("Gamerules", f"Could not read gamerules:\n{e}")
            return
        running = bool(acc.is_running())
        dlg = tk.Toplevel(self.root)
        dlg.title(f"Gamerules — {active}")
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=f"Gamerules for Active World: {active}",
                  font=self.font(10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        note = ("Changes are sent live to the running Server." if running
                else "Server is stopped — values shown are from the last save. Start the Server to change them.")
        ttk.Label(frm, text=note, font=self.font(8),
                  foreground=self.fg("#4CAF50" if running else "#FF9800")).grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))
        state = tk.NORMAL if running else tk.DISABLED
        r = 2
        for rule, (kind, default) in COMMON_GAMERULES.items():
            ttk.Label(frm, text=rule).grid(row=r, column=0, sticky="w", padx=(0, 12), pady=2)
            if kind == "bool":
                var = tk.BooleanVar(value=bool(current.get(rule, default)))
                self.check(frm, variable=var, state=state,
                                command=lambda rl=rule, v=var, k=kind: self._send_gamerule(rl, v, k)).grid(row=r, column=1, sticky="w")
            else:
                var = tk.IntVar(value=int(current.get(rule, default)))
                ttk.Spinbox(frm, from_=0, to=100, width=6, textvariable=var, state=state).grid(row=r, column=1, sticky="w")
                self.button(frm, text="Set", width=5, state=state,
                           command=lambda rl=rule, v=var, k=kind: self._send_gamerule(rl, v, k)).grid(row=r, column=2, sticky="w", padx=4)
            r += 1
        ttk.Label(frm, text="Tip: playerssleepingpercentage 0 = one sleeper is enough to skip the night.",
                  font=self.font(8), foreground=self.fg("gray")).grid(row=r, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.button(frm, text="Close", command=dlg.destroy).grid(row=r + 1, column=0, columnspan=3, pady=(10, 0))

    def _send_gamerule(self, rule: str, var, kind: str):
        acc = self.active_access
        if acc is None or not acc.is_running():
            messagebox.showwarning("Server stopped", "Start the Server to change gamerules.")
            return
        try:
            value = ("true" if var.get() else "false") if kind == "bool" else str(max(0, min(100, int(var.get()))))
        except Exception:
            messagebox.showerror("Gamerules", "Enter a number between 0 and 100.")
            return
        try:
            acc.send_gamerule(rule, value)
        except Exception as e:
            messagebox.showerror("Gamerules", str(e))
            return
        self.console_log(f"> gamerule {rule} {value}")
        self.log(f"gamerule {rule} = {value}", "success")

    def setup_settings_tab(self):
        if self.layout == "menu":   # one row per choice (mockup 2)
            self._setup_settings_tab_menu()
            return
        # Backup policy moved to 💾 Backups and the two update toggles to
        # 🔄 Update (docs/V2-MAJORDOMO-PLAN.md, "Per-server settings leave the
        # Settings tab") -- they're per-Server, so they now live beside the
        # data they configure. This tab is app-level only; adding a Server is
        # the top line's + (3.0).
        self.settings_tab.columnconfigure(0, weight=1)
        location_frame = ttk.LabelFrame(self.settings_tab, text="Current Server's Folder", padding=10)
        location_frame.grid(row=0, column=0, sticky="ew", pady=5)
        location_frame.columnconfigure(1, weight=1)
        ttk.Label(location_frame, text="Server Folder:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.server_entry = ttk.Entry(location_frame)
        self.server_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.server_entry.bind("<KeyRelease>", self.validate_inputs)
        self.button(location_frame, text="Browse", command=self.browse_server).grid(row=0, column=2, padx=5)
        self.server_status = ttk.Label(location_frame, text="", foreground=self.fg("gray"))
        self.server_status.grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(location_frame, text="Relocates the selected Server's folder. To add another Server, use + at the top right.",
                  font=self.font(8), foreground=self.fg("gray")).grid(row=2, column=1, sticky="w", padx=5)
        ui_frame = ttk.LabelFrame(self.settings_tab, text="Interface Settings", padding=10)
        ui_frame.grid(row=1, column=0, sticky="ew", pady=5)
        # Settings ▸ Interface: Classic / New coat of paint / New layout (4.0).
        self._build_interface_row(ui_frame).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(ui_frame, text="Console font size:").grid(row=1, column=0, sticky="w", pady=5)
        self.font_size_var = tk.IntVar(value=self.config.get("console_font_size", 9))
        ttk.Spinbox(ui_frame, from_=6, to=24, width=10, textvariable=self.font_size_var).grid(row=1, column=1, sticky="w", padx=10)
        self.notifications_var = tk.BooleanVar(value=self.config.get("show_notifications", True))
        self.check(ui_frame, text="Show notification messages", variable=self.notifications_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        self.dark_mode_var = tk.BooleanVar(value=self.config.get("dark_mode", False))
        self.check(ui_frame, text="🌙 Dark mode" if self.look == "classic" else "🌙 Dark mode (the new looks are always dark)",
                   variable=self.dark_mode_var, command=self.toggle_dark_mode,
                   state="normal" if self.look == "classic" else "disabled").grid(row=3, column=0, columnspan=2, sticky="w", pady=5)
        self.check_updates_var = tk.BooleanVar(value=self.config.get("check_updates_on_start", True))
        self.check(ui_frame, text="Check for server updates on application start", variable=self.check_updates_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)

        # --- Remote Administration: let another computer running this app
        #     manage THIS machine's Servers over the LAN (docs/V2-MAJORDOMO-PLAN.md).
        ra_cfg = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        ra_frame = ttk.LabelFrame(self.settings_tab, text="Remote Administration (this Machine)", padding=10)
        ra_frame.grid(row=2, column=0, sticky="ew", pady=5)
        ra_frame.columnconfigure(1, weight=1)
        self.remote_enabled_var = tk.BooleanVar(value=bool(ra_cfg.get("enabled")))
        self.check(ra_frame, text="Allow this computer to be administered from another PC on the network",
                        variable=self.remote_enabled_var, command=self.toggle_remote_admin).grid(
                        row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(ra_frame, text="Port:").grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.remote_port_var = tk.IntVar(value=ra_cfg.get("port", REMOTE_DEFAULT_PORT))
        ttk.Spinbox(ra_frame, from_=1024, to=65535, width=8, textvariable=self.remote_port_var).grid(
            row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Label(ra_frame, text="Pairing token:").grid(row=2, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.remote_token_var = tk.StringVar(value=ra_cfg.get("token", "") or "(generated when enabled)")
        token_entry = ttk.Entry(ra_frame, textvariable=self.remote_token_var, state="readonly", width=20)
        token_entry.grid(row=2, column=1, sticky="w", pady=(6, 0))
        tbtns = ttk.Frame(ra_frame)
        tbtns.grid(row=2, column=2, sticky="w", padx=6, pady=(6, 0))
        self.button(tbtns, text="📋 Copy", command=self.copy_remote_token, width=8).pack(side=tk.LEFT, padx=2)
        self.button(tbtns, text="🔄 New", command=self.regenerate_remote_token, width=6).pack(side=tk.LEFT, padx=2)
        self.remote_status_label = ttk.Label(ra_frame, text="", font=self.font(8), foreground=self.fg("gray"))
        self.remote_status_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(ra_frame,
                  text="LAN only — enter this machine's address + token on the other PC's ➕ Machine. Don't port-forward this.",
                  font=self.font(8), foreground=self.fg("gray")).grid(row=4, column=0, columnspan=3, sticky="w")

        btn_frame = ttk.Frame(self.settings_tab)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=20)
        self.button(btn_frame, text="💾 Save Settings", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        self.button(btn_frame, text="🔄 Reset to Defaults", command=self.reset_settings).pack(side=tk.LEFT, padx=5)
        self.button(btn_frame, text="📂 Open Config File", command=self.open_config_file).pack(side=tk.RIGHT, padx=5)
        about_frame = ttk.LabelFrame(self.settings_tab, text="About", padding=10)
        about_frame.grid(row=4, column=0, sticky="ew", pady=5)
        ttk.Label(about_frame, text=f"{APP_NAME} {APP_VERSION_LABEL}").pack(anchor="w")
        ttk.Label(about_frame, text=APP_AUTHOR, foreground=self.fg("gray")).pack(anchor="w")
        ttk.Label(about_frame, text="A comprehensive tool for managing Minecraft Bedrock Dedicated Servers.", foreground=self.fg("gray")).pack(anchor="w", pady=(5, 0))
    
    def create_tooltip(self, widget, text):
        # One tooltip at a time, app-wide, and its 2-second timer lives on the
        # root window: the Server strip rebuilds its buttons on every status
        # change, and a tooltip whose button vanished under the mouse got
        # neither its <Leave> nor its widget-owned timer -- it stayed on screen.
        def show_tooltip(event):
            self._hide_tooltip()
            tooltip = tk.Toplevel(self.root)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root+10}+{event.y_root+10}")
            if self.look == "crafty":
                label = tk.Label(tooltip, text=text, bg=CRAFTY["field"], fg=CRAFTY["ink"], font=crafty_font("body"),
                                 relief="solid", bd=2, padx=6, pady=4, justify=tk.LEFT)
            else:
                label = ttk.Label(tooltip, text=text, background="#ffffe0", relief="solid", borderwidth=1, padding=3)
            label.pack()
            self._tooltip = tooltip
            self._tooltip_timer = self.root.after(2000, self._hide_tooltip)
        widget.bind("<Enter>", show_tooltip, add="+")
        for seq in ("<Leave>", "<ButtonPress>", "<Destroy>"):
            widget.bind(seq, lambda e: self._hide_tooltip(), add="+")

    def _hide_tooltip(self):
        timer, self._tooltip_timer = getattr(self, "_tooltip_timer", None), None
        if timer:
            try:
                self.root.after_cancel(timer)
            except tk.TclError:
                pass
        tooltip, self._tooltip = getattr(self, "_tooltip", None), None
        if tooltip is not None:
            try:
                tooltip.destroy()
            except tk.TclError:
                pass
    
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
        if level == "error":
            self.logger.error(message)
        elif level == "warning":
            self.logger.warning(message)
        else:  # "info" / "success" -- the tag only colours the Activity Log
            self.logger.info(message)
    
    def console_log(self, message: str):
        if self.layout == "menu":   # the chat-style console keeps raw lines for its options
            self._console_append(message)
            return
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
        if self.look == "crafty":
            self.root.configure(bg=CRAFTY["void"])
            self._paint_tk_widgets(self.root)
            return
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
        # last_zip_path is no longer restored: since 3.0 the Advanced ZIP is a
        # one-off override, and a remembered old ZIP would hijack the Update
        # button ("from the selected ZIP" instead of the latest release).
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
        elif current is self.main_tab:
            self.refresh_update_status()  # cached 10 min; "Check again" forces it
        elif current is getattr(self, "teleport_tab", None):
            self.refresh_teleport_page()
        self._refresh_menu_highlight()

    def _build_context(self, profile_id: str, server_path: Path) -> "ServerService":
        """Construct a fresh ServerService for a profile and attach the GUI's
        widget callbacks. The service owns the ServerManager/BackupManager and
        the console ring buffer (its console_buffer callback is registered in
        its own __init__). The GUI-side callbacks are keyed to profile_id and
        marshaled onto the tkinter thread: they only touch the console/control
        widgets when THIS profile is the one currently selected, so a Server
        left running in the background (or driven by a remote admin) never
        leaks its output into the console you're viewing -- only the sidebar
        status dot updates for a background Server."""
        profile = self.config.get("server_profiles", {}).get(profile_id, {})
        known = profile.setdefault("known_players", {}) if profile else {}
        service = ServerService(server_path, self.config, known_players=known)
        service.server_manager.add_output_callback(
            lambda line, pid=profile_id: self.root.after(0, lambda: self._on_service_output(pid, line)))
        service.server_manager.add_status_callback(
            lambda status, pid=profile_id: self.root.after(0, lambda: self._on_service_status(pid, status)))
        if self.remote_host is not None and self.remote_host.is_running():
            self.remote_host.watch_service(profile_id, service)
        return service

    def _on_service_output(self, profile_id: str, line: str):
        """A console line from any Server. The per-service ring buffer already
        captured it; only mirror it into the shared console widget (and learn
        players from it) when this profile is the active one."""
        if self.config.get("active_profile") == profile_id:
            self.console_log(line)
            self._scan_console_line(line)
            self._check_transport_warning(line)
            self._check_console_replies(line)
            if self.layout == "menu" and PLAYER_JOIN_LEAVE_RE.search(line):
                self.root.after(50, self.refresh_teleport_page)   # who can be teleported changed

    def _check_transport_warning(self, line: str):
        """BDS 1.26.5x+ accepts players only over NetherNet and warns on the
        console when server.properties isn't set to it. The usual cause is an
        update (by an older manager, or before merge_new_server_properties
        existed) that kept an old server.properties with no `transport` key at
        all -- BDS then falls back to RakNet and clients get a transport error.
        Offer the one-line fix once per Server per session. A deliberate
        `transport=raknet` (the workaround for Mojang bug BDS-23108) is left
        alone."""
        if "not set to nethernet" not in line.lower():
            return
        acc = self.active_access
        if acc is None or id(acc) in self._transport_checked:
            return
        self._transport_checked.add(id(acc))
        # After the current console burst, not in the middle of it.
        self.root.after(0, lambda: self._offer_transport_fix(acc))

    def _offer_transport_fix(self, acc):
        if self.active_access is not acc:
            return
        try:
            if "transport" in acc.read_properties():
                return  # explicitly set (e.g. raknet on purpose) -- respect it
        except Exception:
            return
        if not messagebox.askyesno("NetherNet required",
                "This Bedrock Server Version only lets players connect over NetherNet, but "
                "server.properties has no 'transport' setting, so players get a transport "
                "error.\n\n(This usually happens when an update kept an older "
                "server.properties.)\n\nAdd transport=nethernet now? It takes effect the next "
                "time the Server starts, so restart it afterwards."):
            return
        try:
            ok = acc.write_properties({"transport": "nethernet"})
        except Exception as e:
            messagebox.showerror("Error", f"Could not update server.properties:\n{e}")
            return
        if not ok:
            messagebox.showerror("Error", "Could not update server.properties.")
            return
        self.log("Added transport=nethernet to server.properties — restart the Server to apply.", "success")
        if hasattr(self, 'properties_editor') and not self.properties_editor.has_unsaved_changes():
            self.properties_editor.load_properties()

    def _on_service_status(self, profile_id: str, status: str):
        """A start/stop from any Server. Refresh the sidebar dots always;
        drive the active-Server control widgets only for the active profile."""
        if self.config.get("active_profile") == profile_id:
            self.update_server_status(status)  # updates the control widgets AND the sidebar
        else:
            self.refresh_sidebar()

    def _get_or_create_context(self, profile_id: str, server_path: Path) -> Optional["ServerService"]:
        """Return the ServerService for profile_id, creating it if needed.

        If a service already exists for this profile_id but points at a
        different (still-running) path -- i.e. the Server Folder was changed
        out from under a running Server -- returns None so the caller can
        refuse the change instead of silently orphaning the running process.
        """
        ctx = self.contexts.get(profile_id)
        if ctx is not None:
            if ctx.server_path == server_path:
                return ctx
            if ctx.is_running():
                return None
        ctx = self._build_context(profile_id, server_path)
        self.contexts[profile_id] = ctx
        return ctx

    def _ensure_service(self, profile_id: str) -> Optional["ServerService"]:
        """Return the ServerService for ANY profile, building it at its
        configured path if the local GUI never activated it. Used by the
        remote-admin host, which serves every configured Server -- not just
        the one currently selected in the sidebar."""
        ctx = self.contexts.get(profile_id)
        if ctx is not None:
            return ctx
        profile = self.config.get("server_profiles", {}).get(profile_id)
        if not profile or not profile.get("path"):
            return None
        path = Path(profile["path"])
        if not path.exists():
            return None
        ctx = self._build_context(profile_id, path)
        self.contexts[profile_id] = ctx
        return ctx

    # --- Remote-admin host provider interface (see RemoteAdminHost) -------
    def remote_token(self) -> str:
        ra = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        if not ra.get("token"):
            ra["token"] = generate_pairing_token()
            save_config(self.config)
        return ra["token"]

    def remote_service(self, profile_id):
        return self._ensure_service(profile_id)

    def remote_server_list(self) -> List[dict]:
        out = []
        for pid, profile in self.config.get("server_profiles", {}).items():
            svc = self.contexts.get(pid)
            out.append({"id": pid, "name": profile.get("name", "Server"),
                        "world": _active_world_of(profile),
                        "running": svc.is_running() if svc else False})
        return out

    def remote_machine_info(self) -> dict:
        return {"name": socket.gethostname(), "platform": sys.platform,
                "app_version": APP_VERSION, "servers": self.remote_server_list()}

    # --- Remote Machines the administrator connects OUT to (config store) --
    def add_machine(self, name: str, host: str, port: int, token: str) -> dict:
        machine = {"id": uuid.uuid4().hex[:8], "name": name.strip() or host,
                   "host": host.strip(), "port": int(port), "token": token.strip()}
        self.config.setdefault("machines", []).append(machine)
        save_config(self.config)
        return machine

    def remove_machine(self, machine_id: str):
        machines = self.config.get("machines", [])
        self.config["machines"] = [m for m in machines if m.get("id") != machine_id]
        save_config(self.config)

    # --- Remote-admin host control (Settings > Remote Administration) -----
    def toggle_remote_admin(self, startup: bool = False):
        ra = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        if startup:
            self.remote_enabled_var.set(True)
        enable = self.remote_enabled_var.get()
        if enable:
            port = int(self.remote_port_var.get())
            token = self.remote_token()  # ensures a token exists
            try:
                if self.remote_host and self.remote_host.is_running():
                    self.remote_host.stop()
                self.remote_host = RemoteAdminHost(
                    self, port=port,
                    log=lambda m: self.root.after(0, lambda: self.log(m, "info")))
                self.remote_host.start()
                # Report starts/stops of every Server to Administrators from the
                # beginning -- not only once one of them has touched that Server.
                for pid, ctx in list(self.contexts.items()):
                    self.remote_host.watch_service(pid, ctx)
            except Exception as e:
                self.remote_enabled_var.set(False)
                ra["enabled"] = False
                self.remote_host = None
                save_config(self.config)
                self._refresh_remote_status()
                messagebox.showerror("Remote Administration", str(e))
                return
            ra["enabled"] = True
            ra["port"] = port
            self.remote_token_var.set(token)
        else:
            if self.remote_host:
                self.remote_host.stop()
                self.remote_host = None
            ra["enabled"] = False
        save_config(self.config)
        self._refresh_remote_status()

    def _refresh_remote_status(self):
        if not hasattr(self, 'remote_status_label'):
            return
        if self.remote_host and self.remote_host.is_running():
            self.remote_status_label.config(
                text=f"● Listening on {get_local_ip()}:{self.remote_host.port} — pair another PC using the token.",
                foreground=self.fg("#4CAF50"))
        else:
            self.remote_status_label.config(text="○ Not accepting remote administration.", foreground=self.fg("gray"))

    def copy_remote_token(self):
        token = self.remote_token()
        self.remote_token_var.set(token)
        self.root.clipboard_clear()
        self.root.clipboard_append(token)
        self.log("Pairing token copied to clipboard", "info")

    def regenerate_remote_token(self):
        if not messagebox.askyesno("Regenerate token",
                "Generate a new pairing token?\n\nAny PC that reconnects will need the new token."):
            return
        ra = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        ra["token"] = generate_pairing_token()
        save_config(self.config)
        self.remote_token_var.set(ra["token"])
        self.log("New pairing token generated", "success")

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
            old_path = str(self.contexts[profile_id].server_path)
            messagebox.showwarning("Server Running",
                "The Server at the previous location is still running.\n\n"
                "Stop it before changing the Server Folder.")
            self.server_entry.delete(0, tk.END)
            self.server_entry.insert(0, old_path)
            return
        self.server_manager = ctx.server_manager
        self.backup_manager = ctx.backup_manager
        # This local ServerService is now the uniform access object the tabs use.
        self.active_access = ctx
        self.active_remote = None
        # Force-sync the UI to this context's ACTUAL state -- it may already be
        # running (we're switching back to it), and no status-change callback
        # fires just from being reselected.
        self.update_server_status("running" if self.server_manager.is_running() else "stopped")
        if hasattr(self, 'console_text') and self.layout == "menu":
            self._console_lines = list(ctx.console_snapshot())
            self._rerender_console()
        elif hasattr(self, 'console_text'):
            self.console_text.config(state=tk.NORMAL)
            self.console_text.delete(1.0, tk.END)
            for line in ctx.console_snapshot():
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
            self.server_running_label.config(text="⬤ Running", foreground=self.fg("green"))
            self.server_status_label.config(text="⬤ Server: Running", foreground=self.fg("green"))
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state=tk.DISABLED)
                self.world_hint_label.config(text="Not available until the running Server is stopped")
        else:
            self.server_running_label.config(text="⬤ Stopped", foreground=self.fg("red"))
            self.server_status_label.config(text="⬤ Server: Stopped", foreground=self.fg("red"))
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            if hasattr(self, 'world_combo'):
                self.world_combo.config(state="readonly")
                self.world_hint_label.config(text="")
        self.refresh_sidebar()

    def update_network_info(self):
        acc = self.active_access
        if acc is None:
            self.network_label.config(text="Network: —")
            return
        try:
            port = acc.read_properties().get("server-port", "19132")
        except Exception:
            port = "19132"
        # Local: this machine's LAN IP. Remote: the Machine's host address.
        if self.active_remote:
            host = self.connections.get(self.active_remote[0]).host if self.active_remote[0] in self.connections else "?"
            self.network_label.config(text=f"Network: {host}:{port} (remote)")
        else:
            self.network_label.config(text=f"Network: {get_local_ip()}:{port}")
    
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
        if self.remote_host:
            self.remote_host.stop()
            self.remote_host = None
        for conn in list(self.connections.values()):
            conn.close()
        self.connections.clear()
        running = [(pid, ctx) for pid, ctx in self.contexts.items() if ctx.is_running()]
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
                ctx.stop()
        self.config["window_geometry"] = self.root.geometry()
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
                self.zip_status.config(text=f"✅ {msg} ({size})", foreground=self.fg("green"))
            else:
                self.zip_status.config(text=f"❌ {msg}", foreground=self.fg("red"))
                valid = False
        else:
            self.zip_status.config(text="")
            valid = False
        if server_path:
            p_server_path = Path(server_path)
            is_valid, msg = is_valid_bedrock_server(server_path)
            if is_valid:
                self._server_folder_state = "valid"
                version = detect_server_version(p_server_path)
                self.server_status.config(text=f"✅ {msg} | Version: {version}", foreground=self.fg("green"))
                self.update_server_info()
            elif p_server_path.exists() and p_server_path.is_dir() and not any(p_server_path.iterdir()):
                self._server_folder_state = "empty"
                self.server_status.config(text="❓ Folder is empty (Ready for Install)", foreground=self.fg("blue"))
                valid = False
                is_zip_valid, _ = is_valid_bedrock_zip(zip_path)
                if is_zip_valid and not self.is_updating:
                    if messagebox.askyesno("Empty Folder",
                        f"The folder '{p_server_path.name}' is empty.\n\nWould you like to install the Minecraft server here now?"):
                        self.is_first_install = True
                        self.is_updating = True
                        self._refresh_update_button()
                        self.log(f"Starting fresh installation to {server_path}...", "info")
                        threading.Thread(target=self.perform_update, daemon=True).start()
                        return
            else:
                self._server_folder_state = "invalid"
                self.server_status.config(text=f"❌ {msg}", foreground=self.fg("red"))
                valid = False
        else:
            self._server_folder_state = "none"
            self.server_status.config(text="")
            valid = False
        self._refresh_update_button()
        if hasattr(self, 'properties_editor'):
            self.properties_editor.load_properties()
    
    def update_server_info(self):
        # get_info()/list_worlds() walk the worlds/ folder on disk (get_folder_size)
        # -- can take seconds on a large, actively-played world with the real engine
        # writing to it concurrently, so this must not run on the main thread (was
        # freezing the whole GUI on every Server-tab visit).
        acc = self.active_access
        if acc is None:
            self.info_text.config(text="No Server yet — press + at the top right to create one.")
            return
        self.info_text.config(text="Loading Server info…")

        def worker(acc=acc):
            try:
                info = acc.get_info()
                worlds = acc.list_worlds()
            except Exception as e:
                self.root.after(0, lambda e=e, acc=acc: self._apply_server_info_error(acc, e))
                return
            self.root.after(0, lambda: self._apply_server_info(acc, info, worlds))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_server_info_error(self, acc, e):
        if self.active_access is not acc:
            return
        self.info_text.config(text=f"Could not read this Server: {e}")

    def _apply_server_info(self, acc, info, worlds):
        if self.active_access is not acc:
            return  # Server was switched while this fetch was in flight -- discard.
        installed = info.get("version", "Unknown")
        active = info.get("active_world", "")
        world_versions = {w["name"]: w["version"] for w in worlds}
        world_line = f"Active World: {active}"
        if active and active not in world_versions:
            world_line += " (not generated yet — created on first start)"
        elif active and world_versions.get(active) not in (None, "Unknown"):
            world_line += f" — last run on {world_versions[active]} (won't load on older versions)"
            iv, wv = parse_version_tuple(installed), parse_version_tuple(world_versions[active])
            if iv and wv and wv > iv:
                world_line += "  ⚠ NEWER than installed Bedrock Server Version!"
        where = " (remote)" if self.active_remote else ""
        info_lines = [
            f"Server Name: {info.get('name', 'Unknown')}{where}",
            f"Bedrock Server Version: {installed}",
            world_line,
            f"Game Mode: {info.get('gamemode', 'Unknown')} | Difficulty: {info.get('difficulty', 'Unknown')}",
            f"Max Players: {info.get('max_players', 'Unknown')} | Port: {info.get('port', '19132')}",
            f"Worlds: {info.get('worlds_count', len(worlds))} | Total size: {info.get('worlds_size', '?')}",
        ]
        self.info_text.config(text="\n".join(info_lines))
        # info["running"] is authoritative (the real engine's state, local or
        # from the host), so sync the Start/Stop/status widgets to it here --
        # this is what lets a running remote Server show "Running" without
        # waiting for a status-change event, and self-heals the state on every
        # Server-tab visit.
        self.update_server_status("running" if info.get("running") else "stopped")
        self.refresh_world_combo()
        self.refresh_backup_header(info.get("name", "Server"))  # reuse info -- avoid a second get_info() disk walk
        self.update_network_info()
        self._installed_version = installed
        if hasattr(self, 'update_version_label'):
            self._render_update_status()
    
    def get_preserve_list(self) -> List[str]:
        return [item for item, var in self.preserve_vars.items() if var.get()]
    
    def dry_run(self):
        if self._block_if_remote("Updating the Bedrock Server Version"):
            return
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
        if self._block_if_remote("Updating the Bedrock Server Version"):
            return
        if self.is_updating:
            return
        # A ZIP chosen under Advanced wins; otherwise install the latest release.
        zip_text = self.zip_entry.get().strip()
        use_zip = bool(zip_text) and is_valid_bedrock_zip(zip_text)[0]
        release = None if use_zip else self._latest_release
        if not use_zip and not release:
            messagebox.showwarning("Update",
                "The newest Bedrock Server Version isn't known yet.\n\n"
                "Press 'Check again', or use Advanced to pick a ZIP or enter a version.")
            return
        what = Path(zip_text).name if use_zip else f"Bedrock Server Version {release['version']}"
        server_dir = Path(self.server_entry.get())
        is_first_install = not (server_dir / "server.properties").exists()
        if is_first_install:
            if not messagebox.askyesno("First-Time Install",
                f"No existing server found in:\n{server_dir}\n\nInstall {what} there?"):
                return
            self.is_first_install = True
            self._launch_update(zip_text if use_zip else None, release)
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
                f"Update this Server to {what}?\n{running_note}\nItems to preserve: {len(preserve)}\nBackup will be created first."):
            return
        # The actual stop happens inside perform_update's worker thread,
        # so the UI never freezes while the Server shuts down.
        self._launch_update(zip_text if use_zip else None, release)

    def _launch_update(self, zip_path: Optional[str], release: Optional[Dict[str, str]]):
        """Run the update in a worker: download the release first if needed,
        then the usual perform_update pipeline."""
        self.is_updating = True
        self._refresh_update_button()

        def worker():
            path, version = zip_path, None
            if release:
                version = release["version"]
                try:
                    self.log(f"Downloading Bedrock Server Version {version}...", "info")
                    dl_dir = Path(get_downloads_folder())
                    dl_dir.mkdir(parents=True, exist_ok=True)
                    path = download_bedrock_zip(
                        release["url"], dl_dir,
                        progress_callback=lambda p: self.root.after(
                            0, lambda p=p: self.set_progress(p, f"Downloading {version}… {p:.0f}%")))
                    self.log(f"Downloaded: {path}", "success")
                except Exception as e:
                    self.log(f"Download failed: {e}", "error")
                    self.root.after(0, lambda e=e: messagebox.showerror(
                        "Download failed",
                        f"Could not download {version}:\n{e}\n\nTry again, or use Advanced."))
                    self.is_updating = False
                    self.is_first_install = False
                    self.root.after(0, lambda: self.set_progress(0, "Download failed"))
                    self.root.after(0, self._refresh_update_button)
                    return
            self.perform_update(path, version)
        threading.Thread(target=worker, daemon=True).start()

    # --- Update all Servers (plan §4) --------------------------------------
    def _update_source(self):
        """(zip_path or None, release or None, version or None): an Advanced ZIP
        wins, as for the single Update; otherwise the latest release."""
        zip_text = self.zip_entry.get().strip() if hasattr(self, 'zip_entry') else ""
        if zip_text and is_valid_bedrock_zip(zip_text)[0]:
            return zip_text, None, version_from_zip_name(Path(zip_text).name)
        rel = self._latest_release
        return None, rel, (rel["version"] if rel else None)

    def _update_all_targets(self, version: Optional[str]) -> List[dict]:
        """Every local Server in list order, with what 'Update all' would do:
        action "update", "current" (already on that version) or "skip"
        (reason in 'why'). Settings come from each Server's own profile; the
        selected Server's from the tabs, exactly as its own Update would."""
        active = self.config.get("active_profile")
        out = []
        for n, (pid, profile) in enumerate(self.config.get("server_profiles", {}).items(), 1):
            path = Path(profile.get("path", ""))
            if pid == active and not self.active_remote:
                preserve = self.get_preserve_list()
                settings = self.config
            else:
                items = profile.get("preserve_items") or DEFAULT_PRESERVE_ITEMS
                preserve = [k for k, v in items.items() if v.get("enabled")]
                settings = profile
            t = {"number": n, "pid": pid, "path": path, "preserve": preserve,
                 "compress": settings.get("compress_backups", False),
                 "keep": settings.get("max_backups", 5) if settings.get("auto_cleanup_backups", True) else None,
                 "world": "", "installed": "", "action": "update", "why": ""}
            ok = bool(profile.get("path")) and is_valid_bedrock_server(str(path))[0]
            if not ok or not (path / "server.properties").is_file():
                t.update(action="skip", why="its folder isn't a complete Bedrock server")
            else:
                t["world"] = parse_server_properties(path / "server.properties").get("level-name", "")
                t["installed"] = detect_server_version(path)
                iv, lv = parse_version_tuple(t["installed"]), parse_version_tuple(version or "")
                if "worlds" not in preserve:
                    t.update(action="skip", why="'worlds' isn't ticked under What to back up — update it on its own")
                elif iv and lv and lv <= iv:
                    t["action"] = "current"
            t["label"] = f"Server {n}: {t['world'] or profile.get('name') or 'Server'}"
            out.append(t)
        return out

    def update_all_servers(self):
        """One download, one confirmation listing every Server, then each one in
        turn: a running Server is stopped first and started again afterwards; a
        Server that fails is reported and doesn't stop the others."""
        if self.is_updating or self.active_remote:
            return
        zip_path, release, version = self._update_source()
        if not zip_path and not release:
            messagebox.showwarning("Update all Servers",
                "The newest Bedrock Server Version isn't known yet.\n\n"
                "Press 'Check again', or use Advanced to pick a ZIP or enter a version.")
            return
        targets = self._update_all_targets(version)
        todo = [t for t in targets if t["action"] == "update"]
        if not todo:
            messagebox.showinfo("Update all Servers", "All Servers are already up to date.")
            return
        what = Path(zip_path).name if zip_path else f"Bedrock Server Version {version}"
        lines = []
        for t in targets:
            if t["action"] == "update":
                ctx = self.contexts.get(t["pid"])
                note = ""
                if ctx and ctx.is_running():
                    online = len(ctx.online_players())
                    note = " — running: stopped, then started again"
                    if online:
                        note += f" ({online} player{'s' if online != 1 else ''} online!)"
                lines.append(f"⬆ {t['label']} ({t['installed'] or 'version unknown'}){note}")
            elif t["action"] == "current":
                lines.append(f"✓ {t['label']} — already up to date")
            else:
                lines.append(f"– {t['label']} — skipped: {t['why']}")
        if not messagebox.askyesno("Update all Servers",
                f"Update {len(todo)} Server{'s' if len(todo) != 1 else ''} to {what}?\n\n"
                + "\n".join(lines) +
                "\n\nEach one is backed up first and updated in turn."):
            return
        self.is_updating = True
        self._refresh_update_button()
        threading.Thread(target=self._update_all_worker, args=(zip_path, release, version, todo),
                         daemon=True).start()

    def _update_all_worker(self, zip_path, release, version, todo):
        ui = lambda fn: self.root.after(0, fn)
        log = lambda msg, level="info": ui(lambda: self.log(msg, level))
        results = []  # (label, ok, note, files_changed)
        try:
            if release:
                try:
                    log(f"Downloading Bedrock Server Version {version}...")
                    dl_dir = Path(get_downloads_folder())
                    dl_dir.mkdir(parents=True, exist_ok=True)
                    zip_path = download_bedrock_zip(
                        release["url"], dl_dir, progress_callback=lambda p: ui(
                            lambda p=p: self.set_progress(p, f"Downloading {version}… {p:.0f}%")))
                    log(f"Downloaded: {zip_path}", "success")
                except Exception as e:
                    log(f"Download failed: {e}", "error")
                    ui(lambda e=e: messagebox.showerror("Download failed",
                        f"Could not download {version}:\n{e}\n\nNo Server was changed."))
                    ui(lambda: self.set_progress(0, "Download failed"))
                    return
            total = len(todo)
            for k, t in enumerate(todo):
                label = t["label"]
                ctx = self._ensure_service(t["pid"])
                if ctx is None:
                    results.append((label, False, "its folder is gone", False))
                    continue
                log("=" * 50)
                log(f"UPDATING {label} ({k + 1} of {total})")
                share = lambda p, text=None, k=k, label=label: ui(lambda: self.set_progress(
                    (k + p / 100) / total * 100,
                    f"{label} ({k + 1} of {total}): {text}" if text else None))
                was_running = ctx.is_running()
                ctx.update_in_progress = True
                try:
                    if was_running:
                        share(5, "Stopping Server...")
                        log(f"Stopping {label} nicely before the update...")
                        ctx.server_manager.stop()
                    swap_server_files(t["path"], Path(zip_path), backup_manager=ctx.backup_manager,
                                      preserve=t["preserve"], compress=t["compress"], version=version,
                                      keep_backups=t["keep"], progress=share, log=log)
                    ok, note, changed = True, "", True
                except UpdateError as e:
                    ok, changed = False, e.files_changed
                    note = str(e) if changed else f"{e} (nothing was changed)"
                    log(f"{label}: update FAILED: {note}", "error")
                except Exception as e:  # e.g. the stop itself failed
                    ok, changed, note = False, False, f"{e} (nothing was changed)"
                    log(f"{label}: update FAILED: {note}", "error")
                finally:
                    ctx.update_in_progress = False
                if was_running and (ok or not changed):
                    log(f"Starting {label} again...")
                    if not ctx.start():
                        note = (note + "; " if note else "") + "it didn't start again — start it by hand"
                        log(f"{label}: didn't start again after the update", "warning")
                elif was_running:
                    note += "; it was left stopped"
                if ok:
                    log(f"{label} updated to {version or Path(zip_path).name}", "success")
                results.append((label, ok, note, changed))
            ui(lambda: self.set_progress(100, "Complete!"))
        finally:
            def done():
                self.is_updating = False
                self._refresh_update_button()
                self.validate_inputs()
                self.refresh_backups()
                self.refresh_sidebar()
                if not results:
                    return
                good = [r for r in results if r[1]]
                bad = [r for r in results if not r[1]]
                msg = ""
                if good:
                    msg += f"Updated to {version or 'the new version'}:\n" + "\n".join(
                        f"  ✓ {lbl}{' — ' + note if note else ''}" for lbl, _, note, _ in good)
                if bad:
                    msg += ("\n\n" if msg else "") + "Not updated:\n" + "\n".join(
                        f"  ✗ {lbl} — {note}" for lbl, _, note, _ in bad)
                    if any(changed for *_, changed in bad):
                        msg += "\n\nA Server that was changed can be put back from its backup in 💾 Backups."
                    messagebox.showwarning("Update all Servers", msg)
                else:
                    messagebox.showinfo("Update all Servers", msg)
            ui(done)

    def perform_update(self, zip_path=None, version: Optional[str] = None):
        # zip_path/version come from the automatic flow (start_update downloads
        # the latest release first); otherwise the Advanced ZIP field is used.
        zip_path = Path(zip_path) if zip_path else Path(self.zip_entry.get())
        version = version or version_from_zip_name(zip_path.name)
        server_path = Path(self.server_entry.get())
        preserve = self.get_preserve_list()
        start_time = time.time()
        is_fresh = getattr(self, 'is_first_install', False)
        ctx = self.active_access if isinstance(self.active_access, ServerService) else None
        try:
            if ctx:
                ctx.update_in_progress = True
            self.log("=" * 50, "info")
            self.log("STARTING FRESH INSTALL" if is_fresh else "STARTING SERVER UPDATE", "info")
            self.log("=" * 50, "info")
            if self.server_manager and self.server_manager.is_running():
                self.set_progress(5, "Stopping Server...")
                self.log("Stopping the running Server nicely before the update...", "info")
                self.server_manager.stop()
                self.log("Server stopped.", "success")
            swap_server_files(
                server_path, zip_path, backup_manager=self.backup_manager, preserve=preserve,
                compress=self.config.get("compress_backups", False), version=version, fresh=is_fresh,
                keep_backups=(self.config.get("max_backups", 5)
                              if self.config.get("auto_cleanup_backups", True) else None),
                progress=self.set_progress, log=self.log)
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
            self.root.after(0, lambda e=e: messagebox.showerror("Error", f"Process failed:\n{str(e)}"))
        finally:
            if ctx:
                ctx.update_in_progress = False
            self.is_updating = False
            self.is_first_install = False
            self.root.after(0, self._refresh_update_button)

    def _find_port_conflict(self) -> Optional[str]:
        """Name of another already-running profile bound to the same
        server-port, or None. See docs/V2-MAJORDOMO-PLAN.md, 'Multi-Server
        local' -- several Servers can run at once, but not on the same port."""
        current_profile_id = self.config.get("active_profile")
        current_port = self.parse_server_properties(
            Path(self.server_entry.get()) / "server.properties").get("server-port", "19132")
        for pid, ctx in self.contexts.items():
            if pid == current_profile_id or not ctx.is_running():
                continue
            other_port = ctx.server_port()
            if other_port == current_port:
                return self.config.get("server_profiles", {}).get(pid, {}).get("name", "another Server")
        return None

    def start_server(self):
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Warning", "No server selected.")
            return
        if acc.is_running():
            return
        if getattr(acc, "update_in_progress", False):
            messagebox.showinfo("Being updated",
                "This Server is being updated right now. Start it when the update has finished.")
            return
        # Port-collision guard applies to local Servers (the host enforces its own).
        if not self.active_remote:
            conflict = self._find_port_conflict()
            if conflict:
                messagebox.showerror("Port already in use",
                    f"'{conflict}' is already running on this same port.\n\n"
                    "Stop it first, or change one Server's server-port in 📝 Configuration.")
                return
        if self.layout == "menu":
            self._console_lines = []
            self._rerender_console()
        else:
            self.console_text.config(state=tk.NORMAL)
            self.console_text.delete(1.0, tk.END)
            self.console_text.config(state=tk.DISABLED)
        self.log("Starting server...", "info")
        def do_start(a=acc):
            try:
                ok = a.start()
            except Exception as e:
                self.root.after(0, lambda e=e: self.log(f"Failed to start server: {e}", "error"))
                return
            self.root.after(0, lambda: self.log("Server started" if ok else "Failed to start server",
                                                 "success" if ok else "error"))
        threading.Thread(target=do_start, daemon=True).start()

    def stop_server(self):
        acc = self.active_access
        if acc is None or not acc.is_running():
            return
        self.log("Stopping server...", "info")
        threading.Thread(target=lambda a=acc: a.stop(), daemon=True).start()

    def restart_server(self):
        acc = self.active_access
        if acc is None:
            return
        self.log("Restarting server...", "info")
        def do_restart(a=acc):
            try:
                a.restart()
            except Exception as e:
                self.root.after(0, lambda e=e: self.log(f"Restart failed: {e}", "error"))
        threading.Thread(target=do_restart, daemon=True).start()

    def send_server_command(self, event=None):
        acc = self.active_access
        if acc is None or not acc.is_running():
            return
        cmd = self.cmd_entry.get().strip()
        if cmd:
            self.console_log(f"> {cmd}")
            acc.send_command(cmd)
            self.cmd_entry.delete(0, tk.END)

    def quick_command(self, cmd: str):
        acc = self.active_access
        if acc and acc.is_running():
            self.console_log(f"> {cmd}")
            acc.send_command(cmd)
    
    def copy_server_ip(self):
        acc = self.active_access
        if acc is None:
            return
        try:
            port = acc.read_properties().get("server-port", "19132")
        except Exception:
            port = "19132"
        if self.active_remote and self.active_remote[0] in self.connections:
            ip = self.connections[self.active_remote[0]].host
        else:
            ip = get_local_ip()
        self.root.clipboard_clear()
        self.root.clipboard_append(f"{ip}:{port}")
        self.log(f"Copied to clipboard: {ip}:{port}", "info")
    
    def manual_backup(self):
        acc = self.active_access
        if acc is None:
            messagebox.showwarning("Warning", "No Server selected.")
            return
        preserve = self.get_preserve_list()
        if not preserve:
            messagebox.showwarning("Warning", "No items selected to preserve.")
            return
        self.log("Creating manual backup...", "info")
        self.set_progress(0, "Backing up...")
        def do_backup(a=acc):
            try:
                success, path, backed_up = a.create_backup(
                    preserve, compress=self.config.get("compress_backups", False),
                    progress_callback=lambda p: self.root.after(0, lambda: self.set_progress(p)))
                self.root.after(0, lambda: self.log(f"Backup created: {Path(str(path)).name}", "success"))
                self.root.after(0, lambda: self.set_progress(100, "Backup complete"))
                self.root.after(0, self.refresh_backups)
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Backup created:\n{path}"))
            except Exception as e:
                self.root.after(0, lambda e=e: self.log(f"Backup failed: {str(e)}", "error"))
                self.root.after(0, lambda e=e: messagebox.showerror("Error", f"Backup failed:\n{str(e)}"))
        threading.Thread(target=do_backup, daemon=True).start()
    
    def refresh_backup_header(self, name: Optional[str] = None):
        """Name the Server these backups belong to, so it's clear what gets backed up.

        Pass `name` when the caller already has it (from a get_info() it just
        did) to avoid a second, redundant get_info() disk walk; otherwise this
        fetches it itself in the background (get_info() walks worlds/ for its
        size, which can be slow -- must not block the main thread)."""
        if not hasattr(self, 'backup_header_label'):
            return
        acc = self.active_access
        if acc is None:
            self.backup_header_label.config(text="Backups for: (no Server selected)")
            return
        if name is not None:
            self._apply_backup_header(acc, name)
            return

        def worker(acc=acc):
            try:
                n = acc.get_info().get("name", "Server")
            except Exception:
                n = "Server"
            self.root.after(0, lambda: self._apply_backup_header(acc, n))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_backup_header(self, acc, name):
        if self.active_access is not acc:
            return  # Server was switched while this fetch was in flight -- discard.
        if self.active_remote:
            mname = self._remote_state.get(self.active_remote[0], {}).get("name", "remote Machine")
            self.backup_header_label.config(text=f"Backups for: {name}  —  stored on {mname}")
        elif self.backup_manager:
            # .backup_dir (attribute), not get_backup_dir() -- displaying shouldn't
            # have the side effect of creating the folder.
            self.backup_header_label.config(text=f"Backups for: {name}  —  stored in {self.backup_manager.backup_dir}")
        else:
            self.backup_header_label.config(text=f"Backups for: {name}")

    def refresh_backups(self):
        acc = self.active_access
        if acc is None:
            return
        for item in self.backup_tree.get_children():
            self.backup_tree.delete(item)
        try:
            backups = acc.list_backups()
        except Exception:
            return
        for backup in backups:
            self.backup_tree.insert("", tk.END, values=(backup["name"], backup["date"], backup["size"]),
                                   tags=(str(backup["path"]),))
        self._sync_menu_rows("backups")
    
    def _selected_backup_path(self):
        """The full path (host-side for remote) stored on the selected row's tag."""
        selected = self.backup_tree.selection()
        if not selected:
            return None, None
        item = self.backup_tree.item(selected[0])
        tags = self.backup_tree.item(selected[0], "tags")
        return (tags[0] if tags else item["values"][0]), item["values"][0]

    def restore_selected_backup(self):
        acc = self.active_access
        if acc is None:
            return
        backup_path, backup_name = self._selected_backup_path()
        if not backup_path:
            messagebox.showwarning("Warning", "No backup selected.")
            return
        if not messagebox.askyesno("Confirm Restore", f"Restore from backup:\n{backup_name}\n\nThis will overwrite current files!"):
            return
        if acc.is_running():
            if not messagebox.askyesno("Server Running", "Stop server to restore?"):
                return
            acc.stop()
        self.log(f"Restoring from {backup_name}...", "info")
        def do_restore(a=acc):
            try:
                success, restored = a.restore_backup(backup_path)
                self.root.after(0, lambda: self.log(f"Restored {len(restored)} items", "success"))
                self.root.after(0, lambda: messagebox.showinfo("Success", f"Restored {len(restored)} items"))
            except Exception as e:
                self.root.after(0, lambda e=e: self.log(f"Restore failed: {str(e)}", "error"))
                self.root.after(0, lambda e=e: messagebox.showerror("Error", str(e)))
        threading.Thread(target=do_restore, daemon=True).start()

    def delete_selected_backup(self):
        acc = self.active_access
        if acc is None:
            return
        backup_path, backup_name = self._selected_backup_path()
        if not backup_path:
            return
        if not messagebox.askyesno("Confirm Delete", f"Delete backup:\n{backup_name}?"):
            return
        try:
            ok = acc.delete_backup(backup_path)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        if ok:
            self.log(f"Deleted: {backup_name}", "info")
            self.refresh_backups()

    def open_selected_backup(self):
        if self._block_if_remote("Opening a backup folder"):
            return
        backup_path, _ = self._selected_backup_path()
        if backup_path:
            p = Path(backup_path)
            if p.exists():
                open_folder(p if p.is_dir() else p.parent)

    def cleanup_backups(self):
        if self._block_if_remote("Cleanup of old backups"):
            return
        if not self.backup_manager:
            return
        max_backups = self.config.get("max_backups", 5)
        deleted = self.backup_manager.cleanup_old_backups(max_backups)
        self.log(f"Cleaned up {deleted} old backup(s)", "info")
        self.refresh_backups()
        messagebox.showinfo("Cleanup", f"Removed {deleted} old backup(s)")

    def open_backup_folder(self):
        if self._block_if_remote("Opening the backups folder"):
            return
        if self.backup_manager:
            open_folder(self.backup_manager.get_backup_dir())
        elif self.server_entry.get():
            open_folder(Path(self.server_entry.get()).parent)
    
    def refresh_worlds(self):
        # list_worlds() walks every world folder on disk (get_folder_size per
        # world) -- can take seconds against a large, actively-played world
        # with the real engine writing to it, so this must not block the main
        # thread (see update_server_info for the same fix / same root cause).
        acc = self.active_access
        if acc is None:
            return
        for item in self.world_tree.get_children():
            self.world_tree.delete(item)

        def worker(acc=acc):
            try:
                worlds_data = acc.list_worlds()
                active = acc.get_active_world()
            except Exception:
                return
            self.root.after(0, lambda: self._apply_worlds(acc, worlds_data, active))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_worlds(self, acc, worlds_data, active):
        if self.active_access is not acc:
            return  # Server was switched while this fetch was in flight -- discard.
        existing = set()
        for world in worlds_data:
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
        self.world_tree.tag_configure("pending", foreground=self.fg("#FF9800"))
        # Bold WITHOUT setting a size/family, so the row keeps the same font as the others.
        base = tkfont.nametofont("TkDefaultFont")
        self._active_bold = tkfont.Font(font=base)
        self._active_bold.configure(weight="bold")
        self.world_tree.tag_configure("active", font=self._active_bold)
        self._sync_menu_rows("worlds")
    
    def on_world_select(self, event):
        selected = self.world_tree.selection()
        if selected:
            item = self.world_tree.item(selected[0])
            self.world_info_label.config(text=f"Selected: {item['values'][0]}")
    
    def open_worlds_folder(self):
        if self._block_if_remote("Opening the worlds folder"):
            return
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
        if self._block_if_remote("Opening the Server folder"):
            return
        if self.server_entry.get():
            open_folder(Path(self.server_entry.get()))
    
    # --- Update tab: latest release, status line, the one Update button -----
    def refresh_update_status(self, force: bool = False):
        """Ask Mojang for the newest release in the background (cached for 10
        minutes unless forced), then redraw the Update tab's status line."""
        if self._latest_check_running:
            return
        fresh = self._latest_checked_at and time.time() - self._latest_checked_at < 600
        if fresh and not force:
            self._render_update_status()
            return
        self._latest_check_running = True
        self._render_update_status()

        def worker():
            try:
                rel, err = fetch_latest_bedrock_release(), None
            except Exception as e:
                rel, err = None, e
            self.root.after(0, lambda: self._apply_latest_release(rel, err))
        threading.Thread(target=worker, daemon=True).start()

    def _apply_latest_release(self, rel, err):
        self._latest_check_running = False
        self._latest_checked_at = time.time()
        if rel:
            self._latest_release, self._latest_error = rel, None
        else:
            self._latest_error = str(err)
            self.log(f"Couldn't check for the latest Bedrock Server Version: {err}", "warning")
        if self._announce_latest and rel:  # startup check: say it once in the Activity Log
            self._announce_latest = False
            iv, lv = parse_version_tuple(self._installed_version), parse_version_tuple(rel["version"])
            if iv and lv > iv:
                self.log(f"A newer Bedrock Server Version is available: {rel['version']} "
                         f"(installed: {self._installed_version}). Update it in 🔄 Update.", "info")
        self._render_update_status()

    def _render_update_status(self):
        if not hasattr(self, 'update_version_label'):
            return
        installed = self._installed_version or "—"
        if self._latest_check_running:
            latest, state, colour = "checking…", "Checking for the newest version…", "gray"
        elif self._latest_release:
            latest = self._latest_release["version"]
            iv, lv = parse_version_tuple(installed), parse_version_tuple(latest)
            if iv and lv and lv <= iv:
                state, colour = "✓ Up to date", "green"
            elif iv:
                state, colour = "A newer version is available.", "#E65100"
            else:
                state, colour = "The installed version isn't known yet (it's read when the Server starts).", "gray"
        elif self._latest_error:
            latest = "unknown"
            state, colour = "Couldn't reach Mojang to check — use Advanced to pick a ZIP or enter a version.", "#C62828"
            self._show_update_advanced(True)
        else:
            latest, state, colour = "—", "", "gray"
        self.update_version_label.config(text=f"Installed: {installed}   ·   Latest: {latest}")
        self.update_state_label.config(text=state, foreground=self.fg(colour))
        self._refresh_update_button()

    def _refresh_update_button(self):
        """One place decides what the Update button says and whether it's enabled."""
        if not hasattr(self, 'update_button'):
            return
        zip_text = self.zip_entry.get().strip() if hasattr(self, 'zip_entry') else ""
        zip_ok = bool(zip_text) and is_valid_bedrock_zip(zip_text)[0]
        rel = self._latest_release
        fresh_install = self._server_folder_state == "empty"
        if self.is_updating:
            text, enabled = "Updating…", False
        elif self.active_remote:
            text, enabled = "Update (runs on the Machine itself)", False
        elif self._server_folder_state not in ("valid", "empty"):
            text, enabled = "Update", False
        elif zip_ok:
            text, enabled = f"🚀 {'Install' if fresh_install else 'Update'} from the selected ZIP", True
        elif rel:
            iv, lv = parse_version_tuple(self._installed_version), parse_version_tuple(rel["version"])
            if fresh_install:
                text, enabled = f"⬇ Install {rel['version']}", True
            elif iv and lv <= iv:
                text, enabled = "✓ Up to date", False
            else:
                text, enabled = f"⬆ Update to {rel['version']}", True
        else:
            text, enabled = "Update", False
        self.update_button.config(text=text, state=tk.NORMAL if enabled else tk.DISABLED)
        self._refresh_update_all_button()

    def _refresh_update_all_button(self):
        if not hasattr(self, 'update_all_button'):
            return
        menu_row = getattr(self, "_update_all_row", None) if self.layout == "menu" else None
        shown = menu_row if menu_row is not None else self.update_all_button
        if len(self.config.get("server_profiles", {})) < 2:
            shown.pack_forget()
            return
        if not shown.winfo_manager():
            if menu_row is not None:   # New layout: the whole row comes and goes
                menu_row.pack(fill=tk.X, after=self._update_this_row)
            else:
                self.update_all_button.pack(side=tk.LEFT, padx=(8, 0), after=self.update_button)
        zip_path, release, version = self._update_source()
        if self.is_updating or self.active_remote or not (zip_path or release):
            text, enabled = "Update all Servers", False
        else:
            n = sum(t["action"] == "update" for t in self._update_all_targets(version))
            if n:
                text, enabled = f"⬆ Update all Servers ({n})", True
            else:
                text, enabled = "✓ All Servers up to date", False
        self.update_all_button.config(text=text, state=tk.NORMAL if enabled else tk.DISABLED)

    def _toggle_update_advanced(self):
        self._show_update_advanced(not self.advanced_frame.winfo_ismapped())

    def _show_update_advanced(self, show: bool):
        if show:
            self.advanced_frame.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            self.advanced_toggle.config(text="Advanced ▾")
        else:
            self.advanced_frame.grid_remove()
            self.advanced_toggle.config(text="Advanced ▸")

    def manual_version_input(self):
        """Fallback for when the automatic check can't reach Mojang: look the
        version up on the Wiki, type it, and the app downloads that ZIP into the
        Advanced field."""
        webbrowser.open("https://minecraft.wiki/w/Bedrock_Dedicated_Server")
        v = simpledialog.askstring("Enter a version",
                                   "Enter the Bedrock Server Version to download\n(e.g. 1.26.52.3):",
                                   parent=self.root)
        if not v:
            return
        version = v.strip().lower().lstrip('v')
        folder = "bin-win" if sys.platform == "win32" else "bin-linux"
        url = f"https://www.minecraft.net/bedrockdedicatedserver/{folder}/bedrock-server-{version}.zip"
        self.log(f"Downloading Bedrock Server Version {version}...", "info")

        def worker():
            try:
                dl_dir = Path(get_downloads_folder())
                dl_dir.mkdir(parents=True, exist_ok=True)
                path = download_bedrock_zip(url, dl_dir, progress_callback=lambda p: self.root.after(
                    0, lambda p=p: self.set_progress(p, f"Downloading {version}… {p:.0f}%")))
            except Exception as e:
                self.log(f"Download failed for version {version}: {e}", "error")
                self.root.after(0, lambda e=e: messagebox.showerror(
                    "Download failed", f"Could not download version {version}:\n{e}\n\nCheck the number and try again."))
                return

            def done():
                self.zip_entry.delete(0, tk.END)
                self.zip_entry.insert(0, str(path))
                self.set_progress(100, "Download finished")
                self.log(f"Downloaded: {path}", "success")
                self.validate_inputs()
            self.root.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def check_for_updates_silent(self):
        """Startup check (Settings: 'Check for server updates on application
        start'): mentions a newer version once in the Activity Log."""
        self._announce_latest = True
        self.refresh_update_status(force=True)
    
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
        messagebox.showinfo("About", f"{APP_NAME}\nVersion {APP_VERSION} \"{APP_RELEASE_NAME}\"\n\n{APP_AUTHOR}\n\nA comprehensive cross-platform tool for managing\nMinecraft Bedrock Dedicated Servers.")


# ============================================================================
# SERVER PROPERTIES EDITOR COMPONENT
# ============================================================================

# ttk.Frame when tkinter is present; object under --agent (this class is never
# instantiated headlessly, but the class statement still runs at import time).
_TkFrameBase = ttk.Frame if TK_AVAILABLE else object


class _CheckValue:
    """A Checkbutton's BooleanVar seen as a properties value ("true"/"false"),
    so a checkbox row fits the editor's key -> .get() map like an Entry."""
    def __init__(self, var):
        self.var = var

    def get(self) -> str:
        return "true" if self.var.get() else "false"


class ServerPropertiesEditor(_TkFrameBase):
    # Settings Bedrock understands but its stock server.properties doesn't list
    # (or only lists commented out), shown as checkboxes at the top:
    # key -> (checkbox text, value when absent, tooltip). One that isn't in the
    # file and still has its absent value is never written, so the file stays
    # as Mojang ships it unless the user actually changes the setting.
    OPTIONAL_CHECKBOXES = {
        "emit-server-telemetry": ("Send server telemetry to Mojang", "false",
                                  "Anonymous gameplay statistics for Mojang. Off unless this is ticked."),
        # Default true per Microsoft's BDS reference: a dedicated server tells
        # clients to fall back from Vibrant Visuals unless this is set to false.
        "disable-client-vibrant-visuals": ("Turn off Vibrant Visuals for players", "true",
                                           "Untick to let players' devices use Vibrant Visuals,\n"
                                           "Minecraft's new graphics mode (on devices that support it)."),
        # Documented only for Minecraft Education; the vanilla engine has the
        # same chat-log code, but it hasn't been verified with real player chat.
        "chat-logging-enabled": ("Save players' chat to a log file (experimental)", "false",
                                 "Writes chat messages to chat_logs/ChatLog_<date>.txt in the Server's folder.\n"
                                 "Experimental: documented by Mojang only for Minecraft Education servers."),
    }

    # Free-text settings that could legitimately hold the word "true" or
    # "false" (a Server named "true", a seed) -- never turned into a checkbox.
    FREE_TEXT_KEYS = {"server-name", "level-seed", "server-ip", "script-debugger-passcode"}

    @classmethod
    def _is_on_off(cls, key: str, value: str) -> bool:
        return key not in cls.FREE_TEXT_KEYS and value.strip().lower() in ("true", "false")

    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.entries = {}
        self._loaded_snapshot = {}
        self._absent_optional = set()
        self.setup_ui()

    def has_unsaved_changes(self) -> bool:
        """Used by the sidebar to guard against silently discarding edits when
        switching Servers (see docs/V2-MAJORDOMO-PLAN.md, 'GUI: sidebar')."""
        if not self.entries:
            return False
        current = {k: e.get() for k, e in self.entries.items()}
        return current != self._loaded_snapshot

    def setup_ui(self):
        if self.app.layout == "menu":
            self._setup_ui_rows()
            return
        header = ttk.Frame(self)
        header.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header, text="Active Server Configuration", font=self.app.font(12, "bold")).pack(side=tk.LEFT)
        btn_frame = ttk.Frame(header)
        btn_frame.pack(side=tk.RIGHT)
        self.app.button(btn_frame, text="🔄 Reload", command=self.load_properties).pack(side=tk.LEFT, padx=2)
        self.app.button(btn_frame, text="💾 Save Changes", command=self.save_properties, style="Primary.TButton").pack(side=tk.LEFT, padx=2)
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
        acc = self.app.active_access
        if acc is None:
            return
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()
        self.entries = {}
        try:
            props = acc.read_properties()
        except Exception:
            self._loaded_snapshot = {}
            return
        if self.app.layout == "menu":
            self._load_rows(props)
            return
        row_index = 0
        self._absent_optional = {k for k in self.OPTIONAL_CHECKBOXES if k not in props}
        for key, (text, default, tip) in self.OPTIONAL_CHECKBOXES.items():
            var = tk.BooleanVar(value=props.get(key, default).strip().lower() == "true")
            ttk.Label(self.scrollable_frame, text=key).grid(row=row_index, column=0, sticky="e", padx=5, pady=2)
            box = self.app.check(self.scrollable_frame, text=text, variable=var)
            box.grid(row=row_index, column=1, sticky="w", padx=5, pady=2)
            self.app.create_tooltip(box, tip)
            self.entries[key] = _CheckValue(var)
            row_index += 1
        for key, value in props.items():
            if key.lower() == "level-name" or key in self.OPTIONAL_CHECKBOXES:
                continue
            ttk.Label(self.scrollable_frame, text=key).grid(row=row_index, column=0, sticky="e", padx=5, pady=2)
            if self._is_on_off(key, value):
                # true/false settings are a checkbox: nothing to mistype.
                var = tk.BooleanVar(value=value.strip().lower() == "true")
                self.app.check(self.scrollable_frame, variable=var).grid(
                    row=row_index, column=1, sticky="w", padx=5, pady=2)
                self.entries[key] = _CheckValue(var)
                row_index += 1
                continue
            entry = ttk.Entry(self.scrollable_frame, width=40)
            entry.insert(0, value)
            entry.grid(row=row_index, column=1, sticky="w", padx=5, pady=2)
            if key == "server-name":
                ttk.Label(self.scrollable_frame, text="(the name players see in their server list)",
                          font=self.app.font(8), foreground=self.app.fg("gray")).grid(row=row_index, column=2, sticky="w", padx=5)
            self.entries[key] = entry
            row_index += 1
        self._loaded_snapshot = {k: e.get() for k, e in self.entries.items()}
        if self.app.look == "crafty":
            self.app._paint_tk_widgets(self.scrollable_frame)

    def save_properties(self):
        acc = self.app.active_access
        if acc is None:
            messagebox.showerror("Error", "No Server selected!")
            return
        new_props = {key: entry.get() for key, entry in self.entries.items()}
        to_write = {k: v for k, v in new_props.items()
                    if not (k in self._absent_optional and v == self.OPTIONAL_CHECKBOXES[k][1])}
        try:
            success = acc.write_properties(to_write)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save properties:\n{e}")
            return
        if success:
            self._loaded_snapshot = dict(new_props)
            self._absent_optional -= set(to_write)
            self._update_savebar()
            self.app.log("Server properties saved successfully.", "success")
            messagebox.showinfo("Success", "Properties saved!")
        else:
            messagebox.showerror("Error", "Failed to save properties.")

    # --- New layout: one row per setting -----------------------------------
    def _setup_ui_rows(self):
        C = CRAFTY
        head = ttk.Frame(self)
        head.pack(fill=tk.X)
        ttk.Label(head, text="CONFIGURATION", font=crafty_font("big")).pack(anchor="w")
        ttk.Label(head, text="Takes effect the next time the Server starts", style="Dim.TLabel").pack(anchor="w", pady=(2, 4))
        bar = ttk.Frame(self, style="Menu.TFrame", padding=(14, 8))
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        self._pending = ttk.Label(bar, text="", style="Menu.TLabel", foreground=C["warn"])
        self._pending.pack(side=tk.LEFT)
        self._save_btn = self.app.button(bar, text="Save changes", style="Primary.TButton", command=self.save_properties)
        self._save_btn.pack(side=tk.RIGHT)
        self._undo_btn = self.app.button(bar, text="Undo", command=self.load_properties)
        self._undo_btn.pack(side=tk.RIGHT, padx=(0, 8))
        container = ttk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, highlightthickness=0, bg=C["pane"])
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = ttk.Frame(canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def wheel(e):
            step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
            canvas.yview_scroll(step * 3, "units")
        for w in (canvas, self.scrollable_frame):
            w.bind("<Enter>", lambda e: (canvas.bind_all("<MouseWheel>", wheel), canvas.bind_all("<Button-4>", wheel),
                                         canvas.bind_all("<Button-5>", wheel)))
            w.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"), canvas.unbind_all("<Button-4>"),
                                         canvas.unbind_all("<Button-5>")))
        self._update_savebar()

    def _load_rows(self, props: Dict[str, str]):
        sf = self.scrollable_frame
        self._absent_optional = {k for k in self.OPTIONAL_CHECKBOXES if k not in props}
        values = dict(props)
        for key, (_text, default, _tip) in self.OPTIONAL_CHECKBOXES.items():
            values.setdefault(key, default)
        groups = {g: [] for g, _ in PROPERTY_GROUPS}
        for key, value in values.items():
            if key.lower() == "level-name":
                continue   # the Active World is chosen on the Server page
            info = PROPERTY_INFO.get(key) or ("Other", key, "", "bool" if self._is_on_off(key, value) else "text", None)
            groups[info[0]].append((key, value, info))
        r = 0
        for group, subtitle in PROPERTY_GROUPS:
            if not groups[group]:
                continue
            ttk.Label(sf, text=group.upper(), font=crafty_font("title")).grid(row=r, column=0, columnspan=2, sticky="w", padx=6, pady=(18, 0))
            ttk.Label(sf, text=subtitle, style="Dim.TLabel").grid(row=r + 1, column=0, columnspan=2, sticky="w", padx=6, pady=(0, 6))
            r += 2
            for key, value, (_g, title, desc, kind, opt) in groups[group]:
                ttk.Separator(sf).grid(row=r, column=0, columnspan=2, sticky="ew")
                left = ttk.Frame(sf)
                left.grid(row=r + 1, column=0, sticky="w", padx=6, pady=8)
                ttk.Label(left, text=title, font=crafty_font("row")).pack(anchor="w")
                if desc:
                    ttk.Label(left, text=desc, style="Dim.TLabel", wraplength=560, justify=tk.LEFT).pack(anchor="w")
                if title != key:
                    ttk.Label(left, text=key, style="Dim.TLabel", font=crafty_font("small")).pack(anchor="w")
                ctl = ttk.Frame(sf)
                ctl.grid(row=r + 1, column=1, sticky="e", padx=6)
                self.entries[key] = self._make_control(ctl, key, value, kind, opt)
                r += 2
        sf.columnconfigure(0, weight=1)
        self._loaded_snapshot = {k: e.get() for k, e in self.entries.items()}
        self._update_savebar()

    def _make_control(self, ctl, key: str, value: str, kind: str, opt):
        if kind == "slider":
            try:
                v = int(str(value).strip())
            except ValueError:
                kind = "text"
            else:
                lo, hi, unit = opt
                lbl = ttk.Label(ctl, text=f"{v}{unit}", width=10, anchor="e")
                slider = CraftySlider(ctl, min(lo, v), max(hi, v), v, width=300,
                                      command=lambda nv, l=lbl, u=unit: (l.config(text=f"{nv}{u}"), self._update_savebar()))
                slider.pack(side=tk.LEFT)
                lbl.pack(side=tk.LEFT, padx=(8, 0))
                return slider
        if kind == "bool":
            var = tk.BooleanVar(value=str(value).strip().lower() == "true")
            self.app.check(ctl, variable=var, command=self._update_savebar).pack()
            return _CheckValue(var)
        if kind == "choice":
            options = list(opt)
            current = next((o for o in options if o.lower() == str(value).strip().lower()), None)
            if current is None:
                options.append(str(value).strip())
                current = options[-1]
            show = (lambda o: o) if key == "chat-restriction" else (lambda o: o[:1].upper() + o[1:])
            labels = {show(o): o for o in options}
            combo = ttk.Combobox(ctl, state="readonly", values=list(labels), width=14, font=crafty_font("body"))
            combo.set(show(current))
            combo.bind("<<ComboboxSelected>>", lambda e: self._update_savebar())
            combo.pack()
            return _ChoiceValue(combo, labels)
        entry = ttk.Entry(ctl, width=28, font=crafty_font("body"))
        entry.insert(0, value)
        entry.bind("<KeyRelease>", lambda e: self._update_savebar())
        entry.pack()
        return entry

    def _update_savebar(self):
        if not hasattr(self, "_pending"):
            return
        pending = self.has_unsaved_changes()
        self._pending.config(text="● Unsaved changes" if pending else "")
        self._save_btn.state(["!disabled"] if pending else ["disabled"])
        self._undo_btn.state(["!disabled"] if pending else ["disabled"])


class _ChoiceValue:
    """A dropdown shown with friendly labels but saved as the file's own value."""
    def __init__(self, combo, labels: Dict[str, str]):
        self.combo, self.labels = combo, labels

    def get(self) -> str:
        shown = self.combo.get()
        return self.labels.get(shown, shown)


# ============================================================================
# HEADLESS AGENT
# ============================================================================

class AgentApp:
    """Headless host: no tkinter, no GUI. Serves this machine's configured
    Servers to remote administrators. Launched with `--agent`. Implements the
    same provider interface RemoteAdminHost consumes as BedrockUpdaterApp does,
    but builds plain (widget-free) ServerServices and blocks until signalled."""

    def __init__(self, config: dict):
        self.config = config
        self.services: Dict[str, ServerService] = {}
        self.host = None

    def _log(self, msg: str):
        print(msg, flush=True)
        try:
            logging.getLogger(__name__).info(msg)
        except Exception:
            pass

    # --- provider interface ---------------------------------------------
    def remote_token(self) -> str:
        ra = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        if not ra.get("token"):
            ra["token"] = generate_pairing_token()
            save_config(self.config)
        return ra["token"]

    def remote_service(self, profile_id):
        if profile_id in self.services:
            return self.services[profile_id]
        profile = self.config.get("server_profiles", {}).get(profile_id)
        if not profile or not profile.get("path"):
            return None
        path = Path(profile["path"])
        if not path.exists():
            return None
        known = profile.setdefault("known_players", {})
        service = ServerService(path, self.config, known_players=known)
        self.services[profile_id] = service
        return service

    def remote_server_list(self) -> List[dict]:
        out = []
        for pid, profile in self.config.get("server_profiles", {}).items():
            svc = self.services.get(pid)
            out.append({"id": pid, "name": profile.get("name", "Server"),
                        "world": _active_world_of(profile),
                        "running": svc.is_running() if svc else False})
        return out

    def remote_machine_info(self) -> dict:
        return {"name": socket.gethostname(), "platform": sys.platform,
                "app_version": APP_VERSION, "servers": self.remote_server_list()}

    def run(self, port: int):
        self.host = RemoteAdminHost(self, port=port, log=self._log)
        token = self.remote_token()
        self.host.start()
        self._log(f"{APP_NAME} {APP_VERSION_LABEL} agent running.")
        self._log(f"Machine: {socket.gethostname()}  ({len(self.config.get('server_profiles', {}))} Server(s) configured)")
        self._log(f"Pairing token: {token}")
        self._log("LAN only — do not port-forward. Ctrl-C to stop.")
        stop_event = threading.Event()

        def _handle(signum, frame):
            self._log("Shutting down agent...")
            stop_event.set()
        signal.signal(signal.SIGINT, _handle)
        try:
            signal.signal(signal.SIGTERM, _handle)
        except Exception:
            pass
        try:
            while not stop_event.wait(0.5):
                pass
        finally:
            self.host.stop()


# ============================================================================
# ENTRY POINT
# ============================================================================

def run_agent(config_path=None, port=None):
    global _CONFIG_PATH_OVERRIDE
    if config_path:
        _CONFIG_PATH_OVERRIDE = config_path
    # Log to the shared app log (file only; console output is handled by _log).
    try:
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[logging.FileHandler(get_log_dir() / f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
                                          encoding='utf-8')])
    except Exception:
        pass
    config = load_config()
    ra = config.get("remote_admin", {})
    use_port = port or ra.get("port") or REMOTE_DEFAULT_PORT
    AgentApp(config).run(use_port)


# ---------------------------------------------------------------------------
# Single-instance guard (see SINGLE_INSTANCE_PORT)
# ---------------------------------------------------------------------------
def _bring_window_to_front(root):
    """Restore (if minimized) and raise the main window to the foreground.
    The brief -topmost toggle is what reliably surfaces it on both Windows and
    Linux/XFCE without leaving it pinned always-on-top."""
    try:
        root.deiconify()
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(300, lambda: root.attributes("-topmost", False))
    except Exception:
        pass


def _bind_single_instance():
    """Bind the single-instance loopback port. Returns the listening socket if
    this is the first GUI instance, or None if another already holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        s.close()
        return None
    s.listen(5)
    return s


def _serve_single_instance(sock, on_focus):
    """Answer pings from later launches: confirm we're this app (so the later
    launch knows to defer to us and quit) and raise our window. Runs on a daemon
    thread; on_focus is expected to marshal onto the tkinter main thread."""
    def _serve():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return  # socket closed at shutdown
            try:
                data = conn.recv(64)
                if data.strip() == _SI_HELLO.strip():
                    try:
                        conn.sendall(_SI_ACK)
                    except OSError:
                        pass
                    on_focus()
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
    threading.Thread(target=_serve, daemon=True).start()


def _ping_existing_instance() -> bool:
    """Ask an already-running instance to raise its window. Returns True only if
    a running instance of THIS app answered (caller should then exit); False if
    nothing, or something unrelated, is on the port (caller should launch)."""
    try:
        c = socket.create_connection(("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2)
    except OSError:
        return False
    try:
        c.sendall(_SI_HELLO)
        c.settimeout(2)
        return c.recv(len(_SI_ACK)).strip() == _SI_ACK.strip()
    except OSError:
        return False
    finally:
        try:
            c.close()
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} {APP_VERSION_LABEL}")
    parser.add_argument("--agent", action="store_true",
                        help="Run headless as a remote-administration host (no GUI).")
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="Use a specific config file (agent mode).")
    parser.add_argument("--port", type=int, default=None,
                        help="Override the remote-administration port.")
    args = parser.parse_args()

    if args.agent:
        run_agent(config_path=args.config, port=args.port)
        return

    if not TK_AVAILABLE:
        print("tkinter is not available — the GUI can't run here.\n"
              "Run headless with:  python bedrock_updater_linux.py --agent", file=sys.stderr)
        sys.exit(1)

    # Single-instance guard: if a GUI is already running, raise its window and
    # exit rather than starting a rival that would fight over the one config
    # file and the one tracked engine (see SINGLE_INSTANCE_PORT).
    lock = _bind_single_instance()
    if lock is None:
        if _ping_existing_instance():
            return  # handed off to the already-running instance
        # Port held by something unrelated -> launch anyway, without the guard.

    # className sets the window's WM_CLASS so Linux desktops (e.g. GNOME) can match
    # the running window to bedrock-server-manager.desktop and reuse its icon.
    root = tk.Tk(className="bedrock-server-manager")
    app = BedrockUpdaterApp(root)
    if lock is not None:
        # Keep the socket bound for our whole lifetime, and answer later launches.
        root._single_instance_lock = lock
        _serve_single_instance(lock, lambda: root.after(0, lambda: _bring_window_to_front(root)))
    root.mainloop()

# ============================================================================
# EMBEDDED FONTS (Crafty looks)
# ============================================================================
#
# The three pixel fonts the Crafty looks use, kept inside this one file so the app
# stays a single file to copy. load_crafty_fonts() unpacks them beside the settings
# and loads them for this process only. Licences: Silkscreen and Pixelify Sans are
# SIL OFL 1.1, Pixel Operator Mono is CC0 1.0 -- full texts in
# _EMBEDDED_FONT_LICENSES below (also written next to the unpacked fonts) and in LICENSES/.

_EMBEDDED_FONTS = {
    "Silkscreen-Bold.ttf": (
        "AAEAAAARAQAABAAQR0RFRgS6A48AAHKQAAAAZEdQT1NTlHyhAABy9AAAA2JHU1VC8xwFowAAdlgAAAFOT1MvMl3GBHoAAAGY"
        "AAAAYGNtYXBCBMdBAAAFiAAAAnRjdnQgCEITQAAAFrwAAABSZnBnbWIu/XwAAAf8AAAODGdhc3AAAAAQAAByiAAAAAhnbHlm"
        "0saTeAAAGNwAAFNEaGVhZPrs3CcAAAEcAAAANmhoZWEH8QJcAAABVAAAACRobXR4wwlodwAAAfgAAAOQbG9jYTfLIuAAABcQ"
        "AAABym1heHACGw6ZAAABeAAAACBuYW1lXyiFcAAAbCAAAAQIcG9zdDxp6T8AAHAoAAACYHByZXB0Qd0yAAAWCAAAALIAAQAA"
        "AAEAQr3NB09fDzz1AA8D6AAAAAC5GPoPAAAAAN7HngX/Bv8GBOID6AABAAYAAgAAAAAAAAABAAAEBv8GAAAFX/8G/RIE4gAB"
        "AAAAAAAAAAAAAAAAAAAA5AABAAAA5AAgAAQAHAAEAAIAJABOAI0AAAB7DgwAAgABAAQDOgK8AAUAAAKKAlgAAABLAooCWAAA"
        "AV4AMgD6AAAAAAAAAAAAAAAAgAAAbwAAAAoAAAAAAAAAAFVOS04AoAANISIEBv8GAAAD6AD6AAAAAQAAAAAB9AK8AAAAIAAD"
        "AnEAfQNrAH0DawB9A2sAfQNrAH0DawB9A2sAfQNrAH0D6AB9A2sAfQNrAH0DawB9A2sAfQPoAH0C7gB9Au4AfQLuAH0C7gB9"
        "Au4AfQLuAH0DawB9A2sAfQH0AH0B9AB9AfQAAAH0AAAB9AAAA2sAfQNrAH0C7gB9A+gAfQPoAH0D6AB9A2sAfQNrAH0DawB9"
        "A2sAfQNrAH0D6AB9A2sAfQPoAH0DawB9Au4AfQNrAH0DawB9A2sAfQNrAH0C7gB9A2sAfQNrAH0DawB9A2sAfQNrAH0D6AB9"
        "A+gAfQPoAH0D6AB9A+gAfQPoAH0C7gB9Au4AfQNrAH0DawB9A2sAfQNrAH0DawB9A2sAfQNrAH0D6AB9A2sAfQNrAH0DawB9"
        "A2sAfQPoAH0C7gB9Au4AfQLuAH0C7gB9Au4AfQLuAH0DawB9A2sAfQH0AH0B9AB9AfQAAAH0AAAB9AAAA2sAfQNrAH0C7gB9"
        "A+gAfQPoAH0D6AB9A2sAfQNrAH0DawB9A2sAfQNrAH0D6AB9A2sAfQPoAH0DawB9Au4AfQNrAH0DawB9A2sAfQNrAH0DawB9"
        "Au4AfQNrAH0DawB9A2sAfQNrAH0DawB9A+gAfQPoAH0D6AB9A+gAfQPoAH0D6AB9Au4AfQLuAH0C7gB9Au4AfQNrAH0C7gB9"
        "A2sAfQNrAH0DawB9A2sAfQNrAH0DawB9A2sAfQNrAH0CcQB9Au4AfQLuAH0E4gB9BOIAfQVfAH0B9AAAAAAAAAH0AAAB9AAA"
        "AfQAfQJxAH0B9AB9AnEAfQPoAH0B9AB9AfQAfQNrAH0DawB9AfQAfQH0AH0D6AB9A+gAfQLuAH0C7gB9Au4AfQAA/wYDawB9"
        "BGUAfQNrAH0CcQB9AnEAfQLuAH0C7gB9AnEAfQJxAH0CcQB9Au4AfQLuAH0C7gB9AfQAfQH0AH0D6AB9A+gAfQJxAH0CcQB9"
        "Au4AfQH0AH0D6AB9A+gAfQNrAH0D6AB9A2sAfQTiAH0E4gB9BOIAfQLuAH0B9AB9AfQAfQLuAH0C7gB9Au4AfQPoAH0DawB9"
        "A+gAfQNrAH0D6AB9A+gAfQLuAH0C7gB9Au4AfQLuAH0C7gB9A+gAfQNrAH0DawB9Au4AfQNrAH0D6AB9BGUAfQAAADsAAAB9"
        "AAAAfQAAAH0AAAB9AAAAfQAAAH0C7gA7AnEAfQJxAH0C7gB9A2sAfQNrAH0CcQB9AAAAAgAAAAMAAAAUAAMAAQAAABQABAJg"
        "AAAAMgAgAAQAEgANAC8AOQB+AP8BUwFhAXgBfgGSAsYC3AMEAwgDJyAUIBogHiAiICYgMCA6IKwhIv//AAAADQAgADAAOgCg"
        "AVIBYAF4AX0BkgLGAtwDAAMIAycgEyAYIBwgICAmIDAgOSCsISL//wB/AAAATAAAAAAAAAAA/sIAAP8k/hr+Bf3X/c79teCO"
        "AAAAAAAA4G7gpeB54BrfmwABAAAAMAAAAEwA1AGSAZQAAAGUAAAAAAAAAAAAAAAAAAABiAGMAZAAAAAAAAAAAAAAAAAAjgCV"
        "ALQAnADFANQAuAC1AKQApQCbAMkAkQCfAJAAnQCSAJMAzgDMAM0AlwC3AAEACQAKAAwADgATABQAFQAWABsAHAAdAB4AHwAh"
        "ACkAKwAsAC0ALwAwADUANgA3ADgAOwCoAJ4AqQDSAKMA3gA9AEUARgBIAEoATwBQAFEAUgBXAFgAWQBaAFsAXQBlAGcAaABp"
        "AGwAbQByAHMAdAB1AHgApgC/AKcA0ACPAJYAwwDHAMQAyADAALoA3QC7AHoAsADRAKAAvADiAL4AzwCHAIgA3wDTALkAmQDj"
        "AIYAewCxAIoAiQCLAJgABQACAAMABwAEAAYACAALABIADwAQABEAGgAXABgAGQANACAAJQAiACMAJwAkAMoAJgA0ADEAMgAz"
        "ADkAKgBrAEEAPgA/AEMAQABCAEQARwBOAEsATABNAFYAUwBUAFUASQBcAGEAXgBfAGMAYADLAGIAcQBuAG8AcAB2AGYAdwAo"
        "AGQALgBqADwAeQCuAK8AqgCsAK0AqwDBAMIAmrAALCCwAFVYRVkgIEu4AA5RS7AGU1pYsDQbsChZYGYgilVYsAIlYbkIAAgA"
        "Y2MjYhshIbAAWbAAQyNEsgABAENgQi2wASywIGBmLbACLCMhIyEtsAMsIGSzAxQVAEJDsBNDIGBgQrECFENCsSUDQ7ACQ1R4"
        "ILAMI7ACQ0NhZLAEUHiyAgICQ2BCsCFlHCGwAkNDsg4VAUIcILACQyNCshMBE0NgQiOwAFBYZVmyFgECQ2BCLbAELLADK7AV"
        "Q1gjISMhsBZDQyOwAFBYZVkbIGQgsMBQsAQmWrIoAQ1DRWNFsAZFWCGwAyVZUltYISMhG4pYILBQUFghsEBZGyCwOFBYIbA4"
        "WVkgsQENQ0VjRWFksChQWCGxAQ1DRWNFILAwUFghsDBZGyCwwFBYIGYgiophILAKUFhgGyCwIFBYIbAKYBsgsDZQWCGwNmAb"
        "YFlZWRuwAiWwDENjsABSWLAAS7AKUFghsAxDG0uwHlBYIbAeS2G4EABjsAxDY7gFAGJZWWRhWbABK1lZI7AAUFhlWVkgZLAW"
        "QyNCWS2wBSwgRSCwBCVhZCCwB0NQWLAHI0KwCCNCGyEhWbABYC2wBiwjISMhsAMrIGSxB2JCILAII0KwBkVYG7EBDUNFY7EB"
        "DUOwAmBFY7AFKiEgsAhDIIogirABK7EwBSWwBCZRWGBQG2FSWVgjWSFZILBAU1iwASsbIbBAWSOwAFBYZVktsAcssAlDK7IA"
        "AgBDYEItsAgssAkjQiMgsAAjQmGwAmJmsAFjsAFgsAcqLbAJLCAgRSCwDkNjuAQAYiCwAFBYsEBgWWawAWNgRLABYC2wCiyy"
        "CQ4AQ0VCKiGyAAEAQ2BCLbALLLAAQyNEsgABAENgQi2wDCwgIEUgsAErI7AAQ7AEJWAgRYojYSBkILAgUFghsAAbsDBQWLAg"
        "G7BAWVkjsABQWGVZsAMlI2FERLABYC2wDSwgIEUgsAErI7AAQ7AEJWAgRYojYSBksCRQWLAAG7BAWSOwAFBYZVmwAyUjYURE"
        "sAFgLbAOLCCwACNCsw0MAANFUFghGyMhWSohLbAPLLECAkWwZGFELbAQLLABYCAgsA9DSrAAUFggsA8jQlmwEENKsABSWCCw"
        "ECNCWS2wESwgsBBiZrABYyC4BABjiiNhsBFDYCCKYCCwESNCIy2wEixLVFixBGREWSSwDWUjeC2wEyxLUVhLU1ixBGREWRsh"
        "WSSwE2UjeC2wFCyxABJDVVixEhJDsAFhQrARK1mwAEOwAiVCsQ8CJUKxEAIlQrABFiMgsAMlUFixAQBDYLAEJUKKiiCKI2Gw"
        "ECohI7ABYSCKI2GwECohG7EBAENgsAIlQrACJWGwECohWbAPQ0ewEENHYLACYiCwAFBYsEBgWWawAWMgsA5DY7gEAGIgsABQ"
        "WLBAYFlmsAFjYLEAABMjRLABQ7AAPrIBAQFDYEItsBUsALEAAkVUWLASI0IgRbAOI0KwDSOwAmBCIGC3GBgBABEAEwBCQkKK"
        "YCCwFCNCsAFhsRQIK7CLKxsiWS2wFiyxABUrLbAXLLEBFSstsBgssQIVKy2wGSyxAxUrLbAaLLEEFSstsBsssQUVKy2wHCyx"
        "BhUrLbAdLLEHFSstsB4ssQgVKy2wHyyxCRUrLbArLCMgsBBiZrABY7AGYEtUWCMgLrABXRshIVktsCwsIyCwEGJmsAFjsBZg"
        "S1RYIyAusAFxGyEhWS2wLSwjILAQYmawAWOwJmBLVFgjIC6wAXIbISFZLbAgLACwDyuxAAJFVFiwEiNCIEWwDiNCsA0jsAJg"
        "QiBgsAFhtRgYAQARAEJCimCxFAgrsIsrGyJZLbAhLLEAICstsCIssQEgKy2wIyyxAiArLbAkLLEDICstsCUssQQgKy2wJiyx"
        "BSArLbAnLLEGICstsCgssQcgKy2wKSyxCCArLbAqLLEJICstsC4sIDywAWAtsC8sIGCwGGAgQyOwAWBDsAIlYbABYLAuKiEt"
        "sDAssC8rsC8qLbAxLCAgRyAgsA5DY7gEAGIgsABQWLBAYFlmsAFjYCNhOCMgilVYIEcgILAOQ2O4BABiILAAUFiwQGBZZrAB"
        "Y2AjYTgbIVktsDIsALEAAkVUWLEOBkVCsAEWsDEqsQUBFUVYMFkbIlktsDMsALAPK7EAAkVUWLEOBkVCsAEWsDEqsQUBFUVY"
        "MFkbIlktsDQsIDWwAWAtsDUsALEOBkVCsAFFY7gEAGIgsABQWLBAYFlmsAFjsAErsA5DY7gEAGIgsABQWLBAYFlmsAFjsAEr"
        "sAAWtAAAAAAARD4jOLE0ARUqIS2wNiwgPCBHILAOQ2O4BABiILAAUFiwQGBZZrABY2CwAENhOC2wNywuFzwtsDgsIDwgRyCw"
        "DkNjuAQAYiCwAFBYsEBgWWawAWNgsABDYbABQ2M4LbA5LLECABYlIC4gR7AAI0KwAiVJiopHI0cjYSBYYhshWbABI0KyOAEB"
        "FRQqLbA6LLAAFrAXI0KwBCWwBCVHI0cjYbEMAEKwC0MrZYouIyAgPIo4LbA7LLAAFrAXI0KwBCWwBCUgLkcjRyNhILAGI0Kx"
        "DABCsAtDKyCwYFBYILBAUVizBCAFIBuzBCYFGllCQiMgsApDIIojRyNHI2EjRmCwBkOwAmIgsABQWLBAYFlmsAFjYCCwASsg"
        "iophILAEQ2BkI7AFQ2FkUFiwBENhG7AFQ2BZsAMlsAJiILAAUFiwQGBZZrABY2EjICCwBCYjRmE4GyOwCkNGsAIlsApDRyNH"
        "I2FgILAGQ7ACYiCwAFBYsEBgWWawAWNgIyCwASsjsAZDYLABK7AFJWGwBSWwAmIgsABQWLBAYFlmsAFjsAQmYSCwBCVgZCOw"
        "AyVgZFBYIRsjIVkjICCwBCYjRmE4WS2wPCywABawFyNCICAgsAUmIC5HI0cjYSM8OC2wPSywABawFyNCILAKI0IgICBGI0ew"
        "ASsjYTgtsD4ssAAWsBcjQrADJbACJUcjRyNhsABUWC4gPCMhG7ACJbACJUcjRyNhILAFJbAEJUcjRyNhsAYlsAUlSbACJWG5"
        "CAAIAGNjIyBYYhshWWO4BABiILAAUFiwQGBZZrABY2AjLiMgIDyKOCMhWS2wPyywABawFyNCILAKQyAuRyNHI2EgYLAgYGaw"
        "AmIgsABQWLBAYFlmsAFjIyAgPIo4LbBALCMgLkawAiVGsBdDWFAbUllYIDxZLrEwARQrLbBBLCMgLkawAiVGsBdDWFIbUFlY"
        "IDxZLrEwARQrLbBCLCMgLkawAiVGsBdDWFAbUllYIDxZIyAuRrACJUawF0NYUhtQWVggPFkusTABFCstsEMssDorIyAuRrAC"
        "JUawF0NYUBtSWVggPFkusTABFCstsEQssDsriiAgPLAGI0KKOCMgLkawAiVGsBdDWFAbUllYIDxZLrEwARQrsAZDLrAwKy2w"
        "RSywABawBCWwBCYgICBGI0dhsAwjQi5HI0cjYbALQysjIDwgLiM4sTABFCstsEYssQoEJUKwABawBCWwBCUgLkcjRyNhILAG"
        "I0KxDABCsAtDKyCwYFBYILBAUVizBCAFIBuzBCYFGllCQiMgR7AGQ7ACYiCwAFBYsEBgWWawAWNgILABKyCKimEgsARDYGQj"
        "sAVDYWRQWLAEQ2EbsAVDYFmwAyWwAmIgsABQWLBAYFlmsAFjYbACJUZhOCMgPCM4GyEgIEYjR7ABKyNhOCFZsTABFCstsEcs"
        "sQA6Ky6xMAEUKy2wSCyxADsrISMgIDywBiNCIzixMAEUK7AGQy6wMCstsEkssAAVIEewACNCsgABARUUEy6wNiotsEossAAV"
        "IEewACNCsgABARUUEy6wNiotsEsssQABFBOwNyotsEwssDkqLbBNLLAAFkUjIC4gRoojYTixMAEUKy2wTiywCiNCsE0rLbBP"
        "LLIAAEYrLbBQLLIAAUYrLbBRLLIBAEYrLbBSLLIBAUYrLbBTLLIAAEcrLbBULLIAAUcrLbBVLLIBAEcrLbBWLLIBAUcrLbBX"
        "LLMAAABDKy2wWCyzAAEAQystsFksswEAAEMrLbBaLLMBAQBDKy2wWyyzAAABQystsFwsswABAUMrLbBdLLMBAAFDKy2wXiyz"
        "AQEBQystsF8ssgAARSstsGAssgABRSstsGEssgEARSstsGIssgEBRSstsGMssgAASCstsGQssgABSCstsGUssgEASCstsGYs"
        "sgEBSCstsGcsswAAAEQrLbBoLLMAAQBEKy2waSyzAQAARCstsGosswEBAEQrLbBrLLMAAAFEKy2wbCyzAAEBRCstsG0sswEA"
        "AUQrLbBuLLMBAQFEKy2wbyyxADwrLrEwARQrLbBwLLEAPCuwQCstsHEssQA8K7BBKy2wciywABaxADwrsEIrLbBzLLEBPCuw"
        "QCstsHQssQE8K7BBKy2wdSywABaxATwrsEIrLbB2LLEAPSsusTABFCstsHcssQA9K7BAKy2weCyxAD0rsEErLbB5LLEAPSuw"
        "QistsHossQE9K7BAKy2weyyxAT0rsEErLbB8LLEBPSuwQistsH0ssQA+Ky6xMAEUKy2wfiyxAD4rsEArLbB/LLEAPiuwQSst"
        "sIAssQA+K7BCKy2wgSyxAT4rsEArLbCCLLEBPiuwQSstsIMssQE+K7BCKy2whCyxAD8rLrEwARQrLbCFLLEAPyuwQCstsIYs"
        "sQA/K7BBKy2whyyxAD8rsEIrLbCILLEBPyuwQCstsIkssQE/K7BBKy2wiiyxAT8rsEIrLbCLLLILAANFUFiwBhuyBAIDRVgj"
        "IRshWVlCK7AIZbADJFB4sQUBFUVYMFktAEu4AMhSWLEBAY5ZsAG5CAAIAGNwsQAHQrMAHgIAKrEAB0K1IwQRCgIKKrEAB0K1"
        "JwIbBgIKKrEACUK7CQAEgAACAAsqsQALQrsAQADAAAIACyq5AAMAAESxJAGIUViwQIhYuQADAGREsSgBiFFYuAgAiFi5AAMA"
        "AERZG7EnAYhRWLoIgAABBECIY1RYuQADAABEWVlZWVm1JQIVBgIOKrgB/4WwBI2xAgBEswVkBgBERAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAA+gD6AXcAXQCCAIIBdwBdAnEAAAJxAnEAAAAAAnEAAAJxAnEAAAAAABgAGAAYABgCcQB9AnEAfQAA"
        "AAAAKABeAGoAwADSAN4BNAFAAXIBrgHqAfYCJgJmApICngKqAugC9AMcA1IDegOUA6ADrAPWA+IEDgRoBIYErgTgBOwFIgUu"
        "BYQFlgWiBegF9AYqBlwGhgbGBwAHPgegB8IH7Af4CEQIVghiCJQIvgkoCXAJfAncCjYKtArqCvYLTAteC2oLwAvMC/4MOgx2"
        "DIIMsgzyDR4NKg02DXQNgA2oDd4OBg4gDlIOlg7ADvIPHg94D5YPvg/wD/wQMhA+EJQQphCyEPgRBBE6EWwRlhHWEhASThKw"
        "EvYTGBNCE04TmhOsE7gT6hQUFH4UxhTSFTIVjBYKFi4WVhaMFrIXChdIF3AXphfmGDYYhhjGGOYZNBl8Ge4aKBqYGpgamBqY"
        "GpgasBrSGvwbLhtGG24bmBvUHBIcLhxKHIAcuBz+HUQdYB1qHYYdoh3CHe4eGh5uHsIe5h8KHywfRB9eH3gfkh+sH+4gMCBe"
        "IIwgpiDAIPQhKiGeIcYiICJ2IsojBiM0I1AjeCOgI9YkBiQwJKQk9CUmJYIlqiXWJg4mOiaaJvwnKidUJ4YnwCfwKBgoQihu"
        "KJYovij4KSIpQilqKXIpeimCKYopkimaKaIAAAACAH0AAAH0AnEAAwAHACpAJwAAAAMCAANnAAIBAQJXAAICAV8EAQECAU8A"
        "AAcGBQQAAwADEQUGFyszESERJzMRI30Bd/p9fQJx/Y99AXcAAAIAfQAAAu4CcQALAA8ANUAyAAYABAMGBGcABwcBXwABARVN"
        "AgEAAANfCAUCAwMWA04AAA8ODQwACwALEREREREJBxsrMxEzNSEVMxEjNSMVETM1I319AXd9+n19fQH0fX3+DPr6AXd4AAAA"
        "//8AfQAAAu4D6AImAAEAAAAGANh9AAAAAAMAfQAAAu4D6AALABcAGwBYQFUAAQAEAAEEZwIBAA4FAgMHAANnAAwACgkMCmcA"
        "DQ0HXwAHBxVNCAEGBglfDwsCCQkWCU4MDAAAGxoZGAwXDBcWFRQTEhEQDw4NAAsACxEREREREAcbKxM1MzUhFTMVIzUjFQMR"
        "MzUhFTMRIzUjFREzNSN9fQF3ffp9+n0Bd336fX19Au59fX19fX39EgH0fX3+DPr6AXd4AP//AH0AAALuA2sCJgABAAABBgDW"
        "QoMACbECArj/g7A1KwAAAP//AH0AAALuA+gCJgABAAAABgDXfQAAAAADAH0AAALuA+gACwAXABsAWEBVAwEBBAEAAgEAZwAC"
        "DgEFBwIFZwAMAAoJDApnAA0NB18ABwcVTQgBBgYJXw8LAgkJFglODAwAABsaGRgMFwwXFhUUExIREA8ODQALAAsRERERERAH"
        "GysTNSM1MxUzNTMVIxUBETM1IRUzESM1IxURMzUj+n36ffp9/gx9AXd9+n19fQLufX19fX19/RIB9H19/gz6+gF3eAD//wB9"
        "AAAC7gPoAiYAAQAAAAYA2gAAAAAAAQB9AAADawJxAA0AOEA1AAACAwIAA4AAAwAEBQMEZwACAgFfAAEBFU0ABQUGYAcBBgYW"
        "Bk4AAAANAA0REREREREIBxwrMxEzNSEVIxUzFSMVMxV9fQJx+vr6+gH0fX19fX19AAMAfQAAAu4CcQAHAAsADwA9QDoABAAH"
        "AgQHZwABAAIGAQJnAAUFAF8AAAAVTQAGBgNfCAEDAxYDTgAADw4NDAsKCQgABwAHERERCQcZKzMRIRUzESMVAzM1IxEzNSN9"
        "AfR9ffp4eHh4AnF9/ol9AXd9/ol9AAAAAAEAfQAAAu4CcQATAD1AOgMBAQAEBwEEZwAHCAEABgcAZwAFBQJfAAICFU0ABgYJ"
        "XwoBCQkWCU4AAAATABMRERERERERERELBx8rMzUjETM1IRUzFSM1IxEzNTMVIxX6fX0Bd336fX36fX0Bd319fX3+iX19fQAA"
        "//8Aff8GAu4CcQImAAoAAAAGANwAAAAAAAIAfQAAAu4CcQAHAAsAMUAuAAEAAgQBAmcABQUAXwAAABVNAAQEA18GAQMDFgNO"
        "AAALCgkIAAcABxEREQcHGSszESEVMxEjFSczESN9AfR9ffp4eAJxff6JfX0BdwAAAAMAfQAAA2sCcQALAA8AEwBBQD4GAQEJ"
        "AQAEAQBnAAMABAgDBGcABwcCXwACAhVNAAgIBV8KAQUFFgVOAAATEhEQDw4NDAALAAsREREREQsHGyszNSM1MzUhFTMRIxUD"
        "MzUjETM1I/p9fQH0fX36fX19ffp9+n3+iX0Bd33+iX0AAQB9AAACcQJxAAsAL0AsAAIAAwQCA2cAAQEAXwAAABVNAAQEBV8G"
        "AQUFFgVOAAAACwALEREREREHBxsrMxEhFSMVMxUjFTMVfQH0+vr6+gJxfX19fX0A//8AfQAAAnED6AImAA4AAAAGANh9AAAA"
        "//8AfQAAAnED6AImAA4AAAAGANkAAAAAAAIAfQAAAnEDawADAA8AREBBAAAIAQECAAFnAAQABQYEBWcAAwMCXwACAhVNAAYG"
        "B18JAQcHFgdOBAQAAAQPBA8ODQwLCgkIBwYFAAMAAxEKBxcrEzUhFQERIRUjFTMVIxUzFX0B9P4MAfT6+vr6Au59ff0SAnF9"
        "fX19ff//AH0AAAJxA+gCJgAOAAAABgDXAAAAAAABAH0AAAJxAnEACQApQCYAAgADBAIDZwABAQBfAAAAFU0FAQQEFgROAAAA"
        "CQAJEREREQYHGiszESEVIxUzFSMVfQH0+vr6AnF9fX36AAAAAAEAfQAAAu4CcQAPADhANQABBQABVwAFBgEABAUAZwADAwJf"
        "AAICFU0ABAQHXwgBBwcWB04AAAAPAA8RERERERERCQcdKzM1IxEzNSEVIREzNTMVIxX6fX0B9P6Jffp9fQF3fX3+ifr6fQAA"
        "AAEAfQAAAu4CcQALACdAJAABAAQDAQRnAgEAABVNBgUCAwMWA04AAAALAAsREREREQcHGyszETMVMzUzESM1IxV9+n36+n0C"
        "cfr6/Y/6+gAAAQB9AAABdwJxAAMAGUAWAAAAFU0CAQEBFgFOAAAAAwADEQMHFyszETMRffoCcf2PAAAA//8AfQAAAfQD6AIm"
        "ABYAAAAGANgAAAAA//8AAAAAAfQD6AImABYAAAAGANmDAAAAAAIAAAAAAfQDawADAAcAKkAnAAAEAQECAAFnAAICFU0FAQMD"
        "FgNOBAQAAAQHBAcGBQADAAMRBgcXKxE1IRUBETMRAfT+ifoC7n19/RICcf2PAAAA//8AAAAAAXcD6AImABYAAAAGANeDAAAA"
        "AAEAfQAAAu4CcQALAC5AKwABAAABVwQBAAADXwADAxVNAAICBV8GAQUFFgVOAAAACwALEREREREHBxsrMzUjNTMVMxEzESMV"
        "+n36ffp9fX19AfT+DH0AAAABAH0AAALuAnEAEwB8S7AKUFhALQAEAQUDBHIABQgGBXAAAQAIBgEIZwADAwBfAgEAABVNAAYG"
        "B2AKCQIHBxYHThtALwAEAQUBBAWAAAUIAQUIfgABAAgGAQhnAAMDAF8CAQAAFU0ABgYHYAoJAgcHFgdOWUASAAAAEwATERER"
        "ERERERERCwcfKzMRMxUzNTMVIxUjFTMVMxUjNSMVffp9+n19fX36fQJxfX19fX19fX19AAABAH0AAAJxAnEABQAfQBwAAAAV"
        "TQABAQJgAwECAhYCTgAAAAUABRERBAcYKzMRMxEzFX36+gJx/gx9AAABAH0AAANrAnEACwAnQCQAAQAEAwEEZwIBAAAVTQYF"
        "AgMDFgNOAAAACwALEREREREHBxsrMxEzFTM1MxEjNSMVffr6+vr6AnF9ff2P+voAAAEAfQAAA2sCcQAPADFALgABAAYFAQZn"
        "AAIABQQCBWcDAQAAFU0IBwIEBBYETgAAAA8ADxEREREREREJBx0rMxEzFTMVMzUzESM1IzUjFX36fX36+n19AnF9ffr9j319"
        "+gAAAP//AH0AAANrA+gCJgAfAAAABgDafQAAAAACAH0AAALuAnEACwAPADVAMgMBAQQBAAUBAGcABwcCXwACAhVNAAYGBV8I"
        "AQUFFgVOAAAPDg0MAAsACxERERERCQcbKzM1IxEzNSEVMxEjFSczESP6fX0Bd319+n19fQF3fX3+iX2CAW0AAP//AH0AAALu"
        "A+gCJgAhAAAABgDYfQAAAAADAH0AAALuA+gACwAXABsAWEBVAAEABAABBGcCAQAOBQIDCAADZwkBBwoBBgsHBmcADQ0IXwAI"
        "CBVNAAwMC18PAQsLFgtODAwAABsaGRgMFwwXFhUUExIREA8ODQALAAsRERERERAHGysTNTM1IRUzFSM1IxUDNSMRMzUhFTMR"
        "IxUnMxEjfX0Bd336fX19fQF3fX36fX0C7n19fX19ff0SfQF3fX3+iX2CAW3//wB9AAAC7gNrAiYAIQAAAQYA1kKDAAmxAgK4"
        "/4OwNSsAAAD//wB9AAAC7gPoAiYAIQAAAAYA130AAAAAAwB9AAADawJxAAsADwATAEhARQAGAQkBBgmAAAkAAQkAfgMBAQQB"
        "AAgBAGcABwcCXwACAhVNAAgIBWAKAQUFFgVOAAATEhEQDw4NDAALAAsREREREQsHGyszNSMRMzUhFTMRIxUBMzUjEzM1I/p9"
        "fQH0fX3+iX19fX19fQF3fX3+iX0Bd33+iX0AAP//AH0AAALuA+gCJgAhAAAABgDaAAAAAAABAH0AAANrAnEADwA5QDYABAAF"
        "AAQFZwABAAAGAQBnAAMDAl8AAgIVTQAGBgdfCAEHBxYHTgAAAA8ADxEREREREREJBx0rMzUjETM1IRUjFTMVIxUzFfp9fQJx"
        "+vr6+n0Bd319fX19fQAAAAACAH0AAALuAnEACQANADVAMgABAAIFAQJnAAUAAwQFA2cABgYAXwAAABVNBwEEBBYETgAADQwL"
        "CgAJAAkRERERCAcaKzMRIRUzFSMVIxURMzUjfQH0fX36eHgCcX19ffoBd30AAQB9AAACcQJxAAsALUAqAAIAAwQCA2cAAQAE"
        "BQEEZwAAABVNBgEFBRYFTgAAAAsACxERERERBwcbKzMRMxUzFTMVIxUjFX36fX19fQJxfX19fX0AAgB9/4MC7gJxAA8AEwA+"
        "QDsEAQIFAQEIAgFnAAYKAQcGB2MACQkDXwADAxVNAAgIAF8AAAAWAE4AABMSERAADwAPEREREREREQsHHSsFNSM1IxEzNSEV"
        "MxEjFTMVJTMRIwH0+n19AXd9fX3+iX19fX19AXd9ff6JfX36AXcAAAACAH0AAALuAnEADQARAD1AOgABAAIHAQJnAAcABQMH"
        "BWcACAgAXwAAABVNAAMDBF8JBgIEBBYETgAAERAPDgANAA0REREREREKBxwrMxEhFTMVIxUzFSM1IxURMzUjfQH0fX19+n19"
        "fQJxfX36fX19AXd9AAEAfQAAAu4CcQATAENAQAADAAIGAwJnAAYAAQcGAWcABwAIAAcIZwAFBQRfAAQEFU0AAAAJXwoBCQkW"
        "CU4AAAATABMRERERERERERELBx8rMzUhNSM1IzUzNSEVIRUzFTMVIxV9AXf6fX0B9P6J+n19fX19fX19fX19fQACAH0AAALu"
        "A+gACwAfAGpAZwMBAQQBAAIBAGcAAhABBQoCBWcACQAIDAkIZwAMAAcNDAdnAA0ADgYNDmcACwsKXwAKChVNAAYGD18RAQ8P"
        "Fg9ODAwAAAwfDB8eHRwbGhkYFxYVFBMSERAPDg0ACwALERERERESBxsrEzUjNTMVMzUzFSMVATUhNSM1IzUzNSEVIRUzFTMV"
        "IxX6ffp9+n3+DAF3+n19AfT+ifp9fQLufX19fX19/RJ9fX19fX19fX19AAAAAQB9AAACcQJxAAcAIUAeAgEAAAFfAAEBFU0E"
        "AQMDFgNOAAAABwAHERERBQcZKzMRIzUhFSMR+n0B9H0B9H19/gwAAAABAH0AAALuAnEACwApQCYEAQAAAV8DAQEBFU0AAgIF"
        "XwYBBQUWBU4AAAALAAsREREREQcHGyszNSMRMxEzETMRIxX6ffp9+n19AfT+DAH0/gx9AP//AH0AAALuA+gCJgAwAAAABgDY"
        "fQAAAAACAH0AAALuA+gACwAXAExASQABAAQAAQRnAgEADAUCAwcAA2cKAQYGB18JAQcHFU0ACAgLXw0BCwsWC04MDAAADBcM"
        "FxYVFBMSERAPDg0ACwALEREREREOBxsrEzUzNSEVMxUjNSMVAzUjETMRMxEzESMVfX0Bd336fX19+n36fQLufX19fX19/RJ9"
        "AfT+DAH0/gx9AAAA//8AfQAAAu4DawImADAAAAEGANZCgwAJsQECuP+DsDUrAAAA//8AfQAAAu4D6AImADAAAAAGANd9AAAA"
        "AAEAfQAAA2sCcQAPADRAMQYBAAMHAwAHgAUBAQECXwQBAgIVTQADAwdfCAEHBxYHTgAAAA8ADxEREREREREJBx0rITUjNSM1"
        "MxUzNTMVIxUjFQF3fX36+vp9fX36+vr6+vp9AAABAH0AAANrAnEACwApQCYEAQAAAV8DAQEBFU0AAgIFXwYBBQUWBU4AAAAL"
        "AAsREREREQcHGyszNSMRMxUzNTMRIxX6ffr6+n19AfR9ff4MfQAAAAABAH0AAANrAnEAGwCMS7AKUFhAMQgBAgUBAwJyCQEB"
        "DAABcAAFAAwABQxnBwEDAwRfBgEEBBVNCgEAAAtgDg0CCwsWC04bQDMIAQIFAQUCAYAJAQEMBQEMfgAFAAwABQxnBwEDAwRf"
        "BgEEBBVNCgEAAAtgDg0CCwsWC05ZQBoAAAAbABsaGRgXFhUUExEREREREREREQ8HHyszNTM1MzUjNSM1MxUzNTMVIxUjFTMV"
        "MxUjNSMVfX19fX36+vp9fX19+vp9fX19fX19fX19fX19fQAAAAEAfQAAA2sCcQAPAF9LsApQWEAgBgEAAwcBAHIFAQEBAl8E"
        "AQICFU0AAwMHXwgBBwcWB04bQCEGAQADBwMAB4AFAQEBAl8EAQICFU0AAwMHXwgBBwcWB05ZQBAAAAAPAA8RERERERERCQcd"
        "KyERIzUjNTMVMzUzFSMVIxEBd319+vr6fX0Bd319fX19ff6J//8AfQAAA2sD6AImADgAAAAHANgA+gAAAAIAfQAAA2sDawAD"
        "ABMAf0uwClBYQCkIAQIFCQMCcgAACgEBBAABZwcBAwMEXwYBBAQVTQAFBQlfCwEJCRYJThtAKggBAgUJBQIJgAAACgEBBAAB"
        "ZwcBAwMEXwYBBAQVTQAFBQlfCwEJCRYJTllAHgQEAAAEEwQTEhEQDw4NDAsKCQgHBgUAAwADEQwHFysTNSEVAREjNSM1MxUz"
        "NTMVIxUjEfoB9P6JfX36+vp9fQLufX39EgF3fX19fX19/okAAAEAfQAAAnECcQAPAIJLsApQWEAxAAQCAQIEcgABBQIBBX4A"
        "BQACBQB+AAAGBgBwAAICA18AAwMVTQAGBgdgCAEHBxYHThtAMwAEAgECBAGAAAEFAgEFfgAFAAIFAH4AAAYCAAZ+AAICA18A"
        "AwMVTQAGBgdgCAEHBxYHTllAEAAAAA8ADxEREREREREJBx0rMzUzNTM1IzUhFSMVIxUzFX19ffoB9H19+vp9fX36fX19AAAA"
        "AAIAfQAAAnED6AAHABcAtUuwClBYQEIMAQMABwADcgAIBgUGCHIABQkGBQl+AAkEBgkEfgAECgoEcAABAgEAAwEAZwAGBgdf"
        "AAcHFU0ACgoLYA0BCwsWC04bQEUMAQMABwADB4AACAYFBggFgAAFCQYFCX4ACQQGCQR+AAQKBgQKfgABAgEAAwEAZwAGBgdf"
        "AAcHFU0ACgoLYA0BCwsWC05ZQCAICAAACBcIFxYVFBMSERAPDg0MCwoJAAcABxEREQ4HGSsTNSM1IRUjFQE1MzUzNSM1IRUj"
        "FSMVMxX6fQH0ff6JfX36AfR9ffoC7n19fX39Evp9fX36fX19AAIAfQAAAu4CcQALAA8ANUAyAAYABAMGBGcABwcBXwABARVN"
        "AgEAAANfCAUCAwMWA04AAA8ODQwACwALEREREREJBxsrMxEzNSEVMxEjNSMVETM1I319AXd9+n19fQH0fX3+DPr6AXd4AAAA"
        "//8AfQAAAu4D6AImAD0AAAAGANh9AAAAAAMAfQAAAu4D6AALABcAGwBYQFUAAQAEAAEEZwIBAA4FAgMHAANnAAwACgkMCmcA"
        "DQ0HXwAHBxVNCAEGBglfDwsCCQkWCU4MDAAAGxoZGAwXDBcWFRQTEhEQDw4NAAsACxEREREREAcbKxM1MzUhFTMVIzUjFQMR"
        "MzUhFTMRIzUjFREzNSN9fQF3ffp9+n0Bd336fX19Au59fX19fX39EgH0fX3+DPr6AXd4AP//AH0AAALuA2sCJgA9AAABBgDW"
        "QoMACbECArj/g7A1KwAAAP//AH0AAALuA+gCJgA9AAAABgDXfQAAAAADAH0AAALuA+gACwAXABsAWEBVAwEBBAEAAgEAZwAC"
        "DgEFBwIFZwAMAAoJDApnAA0NB18ABwcVTQgBBgYJXw8LAgkJFglODAwAABsaGRgMFwwXFhUUExIREA8ODQALAAsRERERERAH"
        "GysTNSM1MxUzNTMVIxUBETM1IRUzESM1IxURMzUj+n36ffp9/gx9AXd9+n19fQLufX19fX19/RIB9H19/gz6+gF3eAD//wB9"
        "AAAC7gPoAiYAPQAAAAYA2gAAAAAAAQB9AAADawJxAA0AOEA1AAACAwIAA4AAAwAEBQMEZwACAgFfAAEBFU0ABQUGYAcBBgYW"
        "Bk4AAAANAA0REREREREIBxwrMxEzNSEVIxUzFSMVMxV9fQJx+vr6+gH0fX19fX19AAMAfQAAAu4CcQAHAAsADwA9QDoABAAH"
        "AgQHZwABAAIGAQJnAAUFAF8AAAAVTQAGBgNfCAEDAxYDTgAADw4NDAsKCQgABwAHERERCQcZKzMRIRUzESMVAzM1IxEzNSN9"
        "AfR9ffp4eHh4AnF9/ol9AXd9/ol9AAAAAAEAfQAAAu4CcQATAD1AOgMBAQAEBwEEZwAHCAEABgcAZwAFBQJfAAICFU0ABgYJ"
        "XwoBCQkWCU4AAAATABMRERERERERERELBx8rMzUjETM1IRUzFSM1IxEzNTMVIxX6fX0Bd336fX36fX0Bd319fX3+iX19fQAA"
        "//8Aff8GAu4CcQImAEYAAAAGANwAAAAAAAIAfQAAAu4CcQAHAAsAMUAuAAEAAgQBAmcABQUAXwAAABVNAAQEA18GAQMDFgNO"
        "AAALCgkIAAcABxEREQcHGSszESEVMxEjFSczESN9AfR9ffp4eAJxff6JfX0BdwAAAAMAfQAAA2sCcQALAA8AEwBBQD4GAQEJ"
        "AQAEAQBnAAMABAgDBGcABwcCXwACAhVNAAgIBV8KAQUFFgVOAAATEhEQDw4NDAALAAsREREREQsHGyszNSM1MzUhFTMRIxUD"
        "MzUjETM1I/p9fQH0fX36fX19ffp9+n3+iX0Bd33+iX0AAQB9AAACcQJxAAsAL0AsAAIAAwQCA2cAAQEAXwAAABVNAAQEBV8G"
        "AQUFFgVOAAAACwALEREREREHBxsrMxEhFSMVMxUjFTMVfQH0+vr6+gJxfX19fX0A//8AfQAAAnED6AImAEoAAAAGANh9AAAA"
        "//8AfQAAAnED6AImAEoAAAAGANkAAAAAAAIAfQAAAnEDawADAA8AREBBAAAIAQECAAFnAAQABQYEBWcAAwMCXwACAhVNAAYG"
        "B18JAQcHFgdOBAQAAAQPBA8ODQwLCgkIBwYFAAMAAxEKBxcrEzUhFQERIRUjFTMVIxUzFX0B9P4MAfT6+vr6Au59ff0SAnF9"
        "fX19ff//AH0AAAJxA+gCJgBKAAAABgDXAAAAAAABAH0AAAJxAnEACQApQCYAAgADBAIDZwABAQBfAAAAFU0FAQQEFgROAAAA"
        "CQAJEREREQYHGiszESEVIxUzFSMVfQH0+vr6AnF9fX36AAAAAAEAfQAAAu4CcQAPADhANQABBQABVwAFBgEABAUAZwADAwJf"
        "AAICFU0ABAQHXwgBBwcWB04AAAAPAA8RERERERERCQcdKzM1IxEzNSEVIREzNTMVIxX6fX0B9P6Jffp9fQF3fX3+ifr6fQAA"
        "AAEAfQAAAu4CcQALACdAJAABAAQDAQRnAgEAABVNBgUCAwMWA04AAAALAAsREREREQcHGyszETMVMzUzESM1IxV9+n36+n0C"
        "cfr6/Y/6+gAAAQB9AAABdwJxAAMAGUAWAAAAFU0CAQEBFgFOAAAAAwADEQMHFyszETMRffoCcf2PAAAAAAIAfQAAAfQD6AAH"
        "AAsANEAxAAEAAgABAmcAAAYBAwQAA2cABAQVTQcBBQUWBU4ICAAACAsICwoJAAcABxEREQgHGSsTNTM1MxUjFQMRMxF9ffp9"
        "+voC7n19fX39EgJx/Y8AAAACAAAAAAH0A+gABwALAFlLsApQWEAcAAEAAAFwAgEABgEDBAADaAAEBBVNBwEFBRYFThtAGwAB"
        "AAGFAgEABgEDBAADaAAEBBVNBwEFBRYFTllAFAgIAAAICwgLCgkABwAHERERCAcZKxE1MzUzFTMVAREzEX36ff6J+gLufX19"
        "ff0SAnH9jwAAAgAAAAAB9ANrAAMABwAqQCcAAAQBAQIAAWcAAgIVTQUBAwMWA04EBAAABAcEBwYFAAMAAxEGBxcrETUhFQER"
        "MxEB9P6J+gLufX39EgJx/Y8AAAAAAgAAAAABdwPoAAcACwA0QDEAAQAAAgEAZwACBgEDBAIDZwAEBBVNBwEFBRYFTggIAAAI"
        "CwgLCgkABwAHERERCAcZKxM1IzUzFTMVAxEzEX19+n36+gLufX19ff0SAnH9jwAAAAEAfQAAAu4CcQALAC5AKwABAAABVwQB"
        "AAADXwADAxVNAAICBV8GAQUFFgVOAAAACwALEREREREHBxsrMzUjNTMVMxEzESMV+n36ffp9fX19AfT+DH0AAAABAH0AAALu"
        "AnEAEwB8S7AKUFhALQAEAQUDBHIABQgGBXAAAQAIBgEIZwADAwBfAgEAABVNAAYGB2AKCQIHBxYHThtALwAEAQUBBAWAAAUI"
        "AQUIfgABAAgGAQhnAAMDAF8CAQAAFU0ABgYHYAoJAgcHFgdOWUASAAAAEwATERERERERERERCwcfKzMRMxUzNTMVIxUjFTMV"
        "MxUjNSMVffp9+n19fX36fQJxfX19fX19fX19AAABAH0AAAJxAnEABQAfQBwAAAAVTQABAQJgAwECAhYCTgAAAAUABRERBAcY"
        "KzMRMxEzFX36+gJx/gx9AAABAH0AAANrAnEACwAnQCQAAQAEAwEEZwIBAAAVTQYFAgMDFgNOAAAACwALEREREREHBxsrMxEz"
        "FTM1MxEjNSMVffr6+vr6AnF9ff2P+voAAAEAfQAAA2sCcQAPADFALgABAAYFAQZnAAIABQQCBWcDAQAAFU0IBwIEBBYETgAA"
        "AA8ADxEREREREREJBx0rMxEzFTMVMzUzESM1IzUjFX36fX36+n19AnF9ffr9j319+gAAAP//AH0AAANrA+gCJgBbAAAABgDa"
        "fQAAAAACAH0AAALuAnEACwAPADVAMgMBAQQBAAUBAGcABwcCXwACAhVNAAYGBV8IAQUFFgVOAAAPDg0MAAsACxERERERCQcb"
        "KzM1IxEzNSEVMxEjFSczESP6fX0Bd319+n19fQF3fX3+iX2CAW0AAP//AH0AAALuA+gCJgBdAAAABgDYfQAAAAADAH0AAALu"
        "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"
        "//8AfQH0Au4CcQAGANsAAP//AH3/BgH0AAAABgDcAAAAAAANAKIAAwABBAkAAACyAAAAAwABBAkAAQAUALIAAwABBAkAAgAI"
        "AMYAAwABBAkAAwA0AM4AAwABBAkABAAeAQIAAwABBAkABQBUASAAAwABBAkABgAeAXQAAwABBAkACAAYAZIAAwABBAkACQAY"
        "AaoAAwABBAkACwAmAcIAAwABBAkADAAmAegAAwABBAkADQEiAg4AAwABBAkADgA2AzAAQwBvAHAAeQByAGkAZwBoAHQAIAAy"
        "ADAAMAAxACAAVABoAGUAIABTAGkAbABrAHMAYwByAGUAZQBuACAAUAByAG8AagBlAGMAdAAgAEEAdQB0AGgAbwByAHMAIAAo"
        "AGgAdAB0AHAAcwA6AC8ALwBnAGkAdABoAHUAYgAuAGMAbwBtAC8AZwBvAG8AZwBsAGUAZgBvAG4AdABzAC8AcwBpAGwAawBz"
        "AGMAcgBlAGUAbgApAFMAaQBsAGsAcwBjAHIAZQBlAG4AQgBvAGwAZAAxAC4AMAAwADEAOwBVAE4ASwBOADsAUwBpAGwAawBz"
        "AGMAcgBlAGUAbgAtAEIAbwBsAGQAUwBpAGwAawBzAGMAcgBlAGUAbgAgAEIAbwBsAGQAVgBlAHIAcwBpAG8AbgAgADEALgAw"
        "ADAAMQA7ACAAdAB0AGYAYQB1AHQAbwBoAGkAbgB0ACAAKAB2ADEALgA4AC4ANAAuADcALQA1AGQANQBiACkAUwBpAGwAawBz"
        "AGMAcgBlAGUAbgAtAEIAbwBsAGQASgBhAHMAbwBuACAASwBvAHQAdABrAGUASgBhAHMAbwBuACAASwBvAHQAdABrAGUAaAB0"
        "AHQAcABzADoALwAvAGsAbwB0AHQAawBlAC4AbwByAGcALwBoAHQAdABwAHMAOgAvAC8AawBvAHQAdABrAGUALgBvAHIAZwAv"
        "AFQAaABpAHMAIABGAG8AbgB0ACAAUwBvAGYAdAB3AGEAcgBlACAAaQBzACAAbABpAGMAZQBuAHMAZQBkACAAdQBuAGQAZQBy"
        "ACAAdABoAGUAIABTAEkATAAgAE8AcABlAG4AIABGAG8AbgB0ACAATABpAGMAZQBuAHMAZQAsACAAVgBlAHIAcwBpAG8AbgAg"
        "ADEALgAxAC4AIABUAGgAaQBzACAAbABpAGMAZQBuAHMAZQAgAGkAcwAgAGEAdgBhAGkAbABhAGIAbABlACAAdwBpAHQAaAAg"
        "AGEAIABGAEEAUQAgAGEAdAA6ACAAaAB0AHQAcABzADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBG"
        "AEwAaAB0AHQAcABzADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBGAEwAAgAAAAAAAP+cADIAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAADkAAAAJADJAMcAYgCtAGMArgCQACUAJgBkACcA6QAoAGUAyADKAMsAKQAqACsALADMAM0AzgDP"
        "AC0ALgAvADAAMQBmADIA0ADRAGcA0wCRAK8AsAAzAO0ANAA1ADYA5AA3ADgA1ADVAGgA1gA5ADoAOwA8AOsAuwA9AOYARABp"
        "AGsAbABqAG4AbQCgAEUARgBvAEcA6gBIAHAAcgBzAHEASQBKAEsATAB0AHYAdwB1AE0ATgBPAFAAUQB4AFIAeQB7AHwAegCh"
        "AH0AsQBTAO4AVABVAFYA5QCJAFcAWAB+AIAAgQB/AFkAWgBbAFwA7AC6AF0A5wCdAJ4AEwAUABUAFgAXABgAGQAaABsAHAEC"
        "AQMBBAD0APUA9gEFAAEAAwEGABEADwAdAB4AqwAEAKMAIgCiAMMAhwANAAYAEgA/ABABBwCyALMAQgALAAwAXgBgAD4AQADE"
        "AMUAtAC1ALYAtwCpAKoAvgC/AAUACgCmACMACQCIAIYAiwCKAIwAgwBfAOgAggDCAIQAvQAHAQgAhQCWAA4A8AC4ACAAIQAf"
        "AJMAYQCkAEEBCQAIAMYBCgELAQwBDQEOAQ8BEACOAEMAjQDYANkA2gDeB3VuaTAwQjkHdW5pMDBCMgd1bmkwMEIzAkNSB3Vu"
        "aTAwQTAHdW5pMDBBRARFdXJvB3VuaTAwQjUHdW5pMDMwOAlncmF2ZWNvbWIJYWN1dGVjb21iB3VuaTAzMDIJdGlsZGVjb21i"
        "B3VuaTAzMDQHdW5pMDMyNwABAAH//wAPAAEAAgAOAAAAAAAAAEgAAgAJAAEABQABAAcADAABAA4ALQABAC8AOwABAD0AQQAB"
        "AEMAUgABAFgAaQABAGsAeAABANYA3AADAAEAAgAAAAwAAAASAAEAAQDcAAIAAQDWANsAAAABAAAACgASACAAAURGTFQAHgAC"
        "bWFyawAabWttawAgAAMAGgAiACwALgAAAAAAAQAAAAAAAgABAAIABAAAAAEAJgAGABAAAQAqAAAABgAQAAEALAABAAD//wAC"
        "AAAAAQABACQALgACAGIAgAABAioCKgABAjACNgABAi4CLgABAjgCUgACAAEA1gDcAAAAAgAIAAEABQAAAAcADAAFAA4ALQAL"
        "AC8AOwArAD0AQQA4AEMAUgA9AFgAaQBNAGsAeABfAAcAAAJ2AAACfAAAAnwAAAKCAAACmgAAApQAAQJqAG0CfAJMAnYCTAJ8"
        "AkwCKAJMAnYCTAJ2AkwCLgJAAnYCTAJ2AkwCdgJSAnYCTAJkAkYCdgJGAnACRgH4AkYCagJGAnACRgJ2AkwCdgJMAgQCFgJq"
        "AhYB/gIWAgQCFgIKAhYCdgJMAnYCTAJwAkYCLgJAAhwCQAIiAkACfAJMAnYCTAJ8AkwCKAJMAnYCTAIuAkACdgJMAi4CQAJ2"
        "AkwCcAJGAnYCTAJ2AkwCfAJMAnACRgJ8AkwCdgJMAnwCTAIoAkwCdgJMAi4CQAIuAkACLgJAAjoCQAIiAkACOgJAAmQCRgJ8"
        "AkwCdgJMAnwCTAIoAkwCdgJMAnYCTAIuAkACdgJMAnYCTAJ2AlICdgJMAi4CQAJkAkYCdgJGAnACRgJkAkYCagJGAnACRgJ2"
        "AhACdgJMAAACFgJ2AkwCcAJGAi4CQAIcAkACIgJAAnwCTAJ2AkwCfAJMAigCTAJ2AkwCLgAAAnYCTAIuAkACdgJMAnACRgJ2"
        "AkwCdgJMAnYCTAJ2AkwCcAJGAnwCTAJ2AkwCfAJMAigCTAJ2AkwCLgJAAi4CQAIuAkACOgJAAjQCQAI6AkACcAJGAAEAAQDc"
        "AAEAAACQAAEAkAACAAEA1gDbAAAABgAAAIgAAACOAAAAjgAAAJQAAACsAAAApgAGAIYAgACAAIYAjACSAAEBegNrAAEA+gPo"
        "AAEA+gJxAAEAvAPoAAEBtv8GAAEA+gAAAAECMwJxAAECMwPoAAEBuQNrAAEB9APoAAECNAPoAAEB9AJxAAEB9AAAAAEBdwAA"
        "AAEBtgAAAAEBOf+DAAEBdALuAAEBOQJxAAEBdwJxAAEBOQPoAAEBdwPoAAEBtgPoAAEBtgJxAAAAAQAAAAoAEgAsAAFERkxU"
        "AC4ABGFhbHQAKmZyYWMAMG9yZG4ANnN1cHMAPAAFACgAMAA4AEAASgBGAAAAAAABAAAAAAABAAIAAAABAAMAAAABAAEAAQAA"
        "AAEAOAABAAAAAQBEAAQAAAABAEIABgAAAAIARABWAAEAAAABAF4AAP//AAQAAAABAAIAAwACAFYABwB6AHsAegB7AIYAhwCI"
        "AAEAVAAJAAEAoAACAFgAXgADAAEAYAABAFgAAAABAAAABAADAAEATgABAFgAAAABAAAABAACAE4ABAB6AHsAegB7AAEABwAB"
        "ACEAPQBdAH0AfgB/AAEAAwB9AH4AfwACADAAOAABADoAAQACAAEAPQACAAEAfACFAAAAAQACACEAXQABAAQAAQAhAD0AXQCK"
        "AAMAnQCAAIkAAwCdAH4AiwADAJ0AgAABAAIAfQB/AAA="
    ),
    "PixelifySans.ttf": (
        "AAEAAAAUAQAABABAR0RFRvVSudsAAJ8wAAABRUdQT1NzrXTyAACgeAAANTxHU1VCpb2yPAAA1bQAAAWASFZBUv5JXrwAANs0"
        "AAACpk1WQVK4eLiqAADd3AAAADNPUy8yY/FWQwAAAcgAAABgU1RBVIQYbN4AAN4QAAAAWGNtYXAL5s36AAALGAAABfhmdmFy"
        "iftpkgAA3mgAAABEZ2FzcAAAABAAAJ8oAAAACGdseWbq0CtsAAAVnAAAdC5ndmFyprJyRgAA3qwAAFaMaGVhZCWkH88AAAFM"
        "AAAANmhoZWEH4QSYAAABhAAAACRobXR4z8J/AAAAAigAAAjubG9jYbUGl58AABEYAAAEgm1heHACTwCKAAABqAAAACBuYW1l"
        "d2sdYAAAicwAAAYhcG9zdKzjpqQAAI/wAAAPNXByZXBoBoyFAAAREAAAAAcAAQAAAAEAABUYh3dfDzz1AAMD6AAAAADhH3Iw"
        "AAAAAOEnah3/2P8IBG4DlwAAAAYAAgAAAAAAAAABAAADmP7oAAAEq//Y/s0EbgABAAAAAAAAAAAAAAAAAAACNwABAAACQABQ"
        "AAcAOAAEAAEAAAAAAAAAAAAAAAAAAwABAAQCOAGQAAUAAAK8AooAAACMArwCigAAAd0AMgD6AAAAAAAAAAAAAAAAoAAC70AA"
        "AFsAAAAAAAAAAE5PTkUAwAAg+wYDmP7oAAADmgEhIAAAnwAAAAABwgK8AAAAIAADAr0AZAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwCSgA8AkoAPAJKADwDtQA8A7UAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKABYCSgAW"
        "AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAGUADwD3gA8AZQAPAGUADwBlAA8AZQAMQGUADwBlAA8AZQAPAGUADwBlAA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwCSgA8Akv/6AL/ADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJvADwCbwA8AkoAPAO1ADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "Ao8APAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "Av8APAL/ADwC/wA8Av8APAL/ADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AqUAPAKlADwCpQA8"
        "AqUAPAKlADwCpQA8AqUAPAKlADwCpQA8AqUAPARsADwEbAA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkkAPAJdADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKAB4CSgA8AN4APADeADwA3gAXAN7/8wDeACYA3v/wAN4APADe//kA3v/lAN7/+ADe/+wCuQA8Ae8APAHvADwB7wA8"
        "AkoAPAJKADwA3gA8AN4AFwDeADwA3gA1AN4APADh/+sC/wA8AkoAPAJKADwCoQAKAkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAFQJKABUCSgA8A7UAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AqUAPAKlADwCpQA8AqUAPAKlADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwCSgA8AkoAPAJAADIC/wA8Av8APAL/ADwC/wA8Av8APAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAJKADwDtQA8A7UAPAO1ADwCSgA8AkoAPARkADwBjgA8AWEAPAJKADwCSgA8AkoAPAHuADwB7gA8Ae4APAKkADwCSgA8"
        "AkoAPAO1ADwCSgA8AkoAPAJKADwCSgA8AkoAPAKkADwC/wA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AkoAPAL/ADwC/wA8AkoAPAJKADwC/wA8Akn/2AKkADwDtQA8AkoAPAJKADwCSgA8ARj/7AJKADwCSf/YA1IAPAJKADwCSf/Y"
        "AqUAPAJKADwCSgA8Ae4APAJKADwCSgA8AqQAPAJKADwCSgA8AvgAPAJKADwCLAA8AkoAPAJKADwCSgA8AqQAPAL/ADwCSgA8"
        "AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAH4AB4CSgA8AkoAPAJXADwC/wA8Av8APAJKADwCSQAAAv8APAJJAAADqwA8"
        "A1IAPAJKADwCSgA8AkoAPADeADwBVAAKAe8APAJKADwDUQA8AkoAPAJnADwCSgA8AkoAPAHuADwCSgA8AkoAPAJKADwCSgA8"
        "AZQAPAJKADwCSgA8Av8APAJKADwBkwA8AkoAPAJKADwCSgA8Av8APAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8"
        "AZQAPAJKADwCSgA8AkkAAAGUAC8CSgA8AkoAPAJKADwCQAAyAkoAPAJKADwCSgA8AkoAPAJKADwA3gA8AkoAPAJKADwCQAAy"
        "AkkAAAJKADwDtQA8AkoAPAJJADwCSgA8AkoAPAJKADwCSgA8AkoAPAL/ADwCSgA8AkoAPAL/ADwCSgA8AkoAPAJKADwCSgA8"
        "AZQAPAJKADwCSgA8AkoAPAJKADwCSgA8AkoAPAJKADwCSgA8AzAAPANxADwDjgA8A20APAO8ADwDaAA8A60APAPLADwD3wA8"
        "AWwAPAEPADwBbAA8AWwAPAFtADwBbQA8AW0APAFtADwBbQA8AW0APAFsADwBDwA8AWwAPAFsADwBbQA8AW0APAFtADwBbQA8"
        "AW0APAFtADwAyAAAAMgAAADeADwBCwA8AN4APAELADwCgwA8AN4APADeADwCSgA8AkoAPADeADwBAgA8AQsAPAGvADwCpQA8"
        "AhEAPAIRADwA3QA8AN0APAFNABcBkwA8AfYAPAG6ADwBdgA8AXYAHgHQAB4B0AAeAXYAPAF2AB4BCwA8AdEAPAHRADwB0QA8"
        "AQsAPAELADwDaQAeA1UACgIsADwCLAAeAdEAPAELADwCSv/gAkoAPAKlADwC/gA8AkoAPAGZAEYBmQBGAxUAPAE5ADwA3gA8"
        "AZMAPASIADwCSgA8AkoAPAHGADwCSgA8AkoAPAJJABYCpAA8BKsAPAKkADwCSgA8AhEAPAJYAEMCWABtAlgAYAJYAG0CWABt"
        "AlgAbQJYAEMCWABDAlgAfgJYAH4CWABlAlgAsAJYALACWABtAQsAPAO1ADwCSQAAAkoAPAJKADwC/wA7AkoAPANjADwCggAe"
        "AAD/+QAAAGMAAAA8AAAATQAAABIAAABhAAAATQAAAE0AAAAdAAAAZAAAABsAAAAJAAAATQAAAFgAAAAfAAAAHwAAABMBLP/3"
        "ASwAbAEKADwBLAA8AAwATQBNABgAXgAbAA0ALAAgAAAAAAACAAAAAwAAABQAAwABAAAAFAAEBeQAAACWAIAABgAWAC8AOQB+"
        "AKUArAE3AX4BkgH/AhsCNwLHAt0DBAMIAwwDEgMoA4YDigOMA48DkwOhA6gDrwO7A7wDwQPJA84EBQQMBB0EOgRDBE8EXARf"
        "BJEehR6eHvMgFCAaIB4gICAiICYgMCA6IDwgcCB5IIkgoSCkIKcgrCEWISIhJiFUIV4iBiIPIhIiFSIeIkgiYCJl+wT7Bv//"
        "AAAAIAAwADoAoACnAK4BOQGSAfwCGAI3AsYC2AMAAwYDCgMSAyYDhgOIA4wDjgORA5UDowOqA7EDvAO9A8MDygQBBAcEDgQg"
        "BDsERARRBF4EkB6AHp4e8iATIBggHCAgICIgJiAwIDkgPCBwIHQggCChIKMgpyCsIRYhIiEmIVMhWyIGIg8iESIVIh4iSCJg"
        "ImT7APsG//8AAAFzAAAAAAAAAAAAAABiAAAAAP6B/3IAAAAAAAAAAP8c/wn99/32/fX99P3W/dX91AAA/dX9tf3U/dMAAAAA"
        "AAAAAAAA/QwAAAAAAAAAAAAA4cIAAOHMAAAAAOHe4bThquHx4bfhnOFQ4VDhNuFg4WLhYOFY4Ong2eD14FvgV+AW4A4AAN/1"
        "3/zfzt+wAAAAAAYDAAEAlgAAALIBOgFEAU4CYAAAAugC7gAAAAAC8AL6AwIDBgAAAAAAAAAAAAAAAAAAAAAAAAL4AAAAAAAA"
        "AAAC+gMCAwoDFAMyAAADZAN6A5ADkgOUAAADnAAAA5wDoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAADfAAAAAAAAAAAA3YDeAAAAAABygHRAfIB2QIDAiAB9gHzAeIB4wHXAgsBzQHeAcwB2gHOAc8CEgIPAhEB0wH1AAEADQAO"
        "ABQAGAAiACMAKAArADYAOAA6AEAAQQBHAFMAVQBWAFoAYQBmAHEAcgB3AHgAfQHmAdsB5wIZAeECNQCBAI0AjgCUAJgAogCj"
        "AKgAqwC3ALoAvADCAMMAygDWANgA2QDdAOQA6QD0APUA+gD7AQAB5AH9AeUCFwHLAdICAAIIAgICCQH4AjMB+QEKAe4CGAH6"
        "Aj0B/AIVAcIBwwI2Ah8B9wHVAj4BwQELAe8BsAGtAbEB1AAGAAIABAAKAAUACQALABEAHwAZABwAHQAyAC0ALwAwABcARQBM"
        "AEgASgBRAEsCDQBPAGsAZwBpAGoAeQBUAOMAhgCCAIQAigCFAIkAiwCRAJ8AmQCcAJ0AsgCtAK8AsACXAMgAzwDLAM0A1ADO"
        "Ag4A0gDuAOoA7ADtAPwA1wD+AAcAhwADAIMACACIAA8AjwASAJIAEwCTABAAkAAVAJUAFgCWACAAoAAaAJoAHgCeACEAoQAb"
        "AJsAJQClACQApAAnAKcAJgCmACoAqgApAKkANQC1ADMAswAuAK4ANAC0ADEArAAsALYANwC5ADkAuwA7AL0APQC/ADwAvgA+"
        "AMAAPwDBAEIAxABEAMcAQwDGAMUARgDJAE4A0QBJAMwATQDQAFIA1QBXANoAWQDcAFgA2wBbAN4AXgDhAF0A4ABcAN8AZADn"
        "AGMA5gBiAOUAcADzAG0A8ABoAOsAbwDyAGwA7wBuAPEAdAD3AHoA/QB7AH4BAQCAAQMAfwECAAwAjABQANMAXwDiAGUA6AI6"
        "AjQCOwI/AjwCNwIkAiUCKAIsAi0CKgIjAiICKwImAikBhAGFAaABoQGiAYABhAGFAZ4BnQGfARQBNwEQATABLwEyATMBLQEu"
        "ATQBGgEiASkBDAENAQ4BDwESARMBFQEWARcBGAEZARsBHAEdAR4BHwEgASEBIwEkASYBJQEnASgBLAErASoBMQE1ATYBOAE5"
        "AToBOwE+AT8BQQFCAUMBRAFFAVEBUgFUAVMBVQFWAVoBWQFYAV8BZAFlAUABZgE8AV4BXQFgAWEBYgFbAVwBYwFGAVABVwER"
        "AT0AdgD5AHMA9gB1APgAfAD/AewB7QHoAeoB6wHpAh4CDAIUAhMBBAEHAQgBBQEGuAH/hbAEjQAAAAAVADgARABQAFwAaAB0"
        "AIAAjACYAKQA2QDlAQkBJwEzAT8BSwFXAWMBggGOAZYBvQHfAesB9wIDAg8CGwInAjMCPwJLAmMChwKTAp8CqwK3AtIC8wL/"
        "AxQDIAMsAzgDRANQA1wDaAN0A38DiwOlA7ED3gPqA/8ECwQXBCMELwRPBHYElQShBK0EuQTFBOwFCQUVBSEFLQU5BUUFUQVd"
        "BYIFjgWaBcwF6QYFBisGTwZbBmcGcwaZBqUGsQa9BskG1Qb5BxIHMwc/B0sHVwdtB3kHhQeRB50HqQe1B8EHzQfZB+UIAwgq"
        "CDYIQghOCFoIhgijCK8IuwjHCNMI9QkBCQ0JGQk6CUYJUgleCWoJdQmBCY0JmQmlCeAJ7AoJCicKMwo/CksKVwpjCoAKjAqx"
        "Ct0K/wsLCxcLIwsvCzsLRwtSC14LaguEC6kLtQvBC80L2QvzDBUMIQw0DEEMTAxjDG4MeQyEDI8MmgyoDLMMzwzkDPkNBQ0m"
        "DTINPw1LDVYNYQ1sDYQNoQ22DcINzQ3ZDeUN8Q4ODisONw5DDk8OWw5mDnIOfg6jDq8Ouw7tDwoPJg9ED1oPZg9yD34PnA+o"
        "D7QPwA/MD9gP+BAWEDsQRxBTEF8QdRCBEI0QmRClELAQvBDIENQQ4BDsEQgRJRExET0RSRFVEYERoBGsEbgRxBHPEewR+BIE"
        "EhASOBJkEo0SqxLGEvcTGxM6E10TgBOkE7QTxBPWE/EUExRAFIYUrBTLFPQVLxVcFXEVmBWwFc0V6xYEFiEWSBZxFp0WtRbQ"
        "FvcXJBc6F1cXexecF7EX1xf1GBsYQRhZGHMYkRi3GNwY/RkgGUUZaRl5GZ0ZuhnUGfYaIxpnGokapxrPGu8bHRsyG1gbbhuL"
        "G54buxvZG/IcERw7HGIcjhylHMAc2xz/HRUdMh1WHXUdlx25Hdcd+R4bHi4eRh5bHncenB7AHuofDR8xH0EfYx+FH6Afwx/Y"
        "IAUgIyBKIGkghCChILQg0SD3IRAhLSFhIY0hrCHYIgMiJCI/ImEihCKnIsgi8CMOIy4jSiNuI5AjtiPLI+0kACQhJD4kWiSD"
        "JKAkxyTkJQElGiU5JWIljiWtJcol5iYIJismTiZ2JpImrybEJuonECcxJ1onfyeVJ74n4ygsKHUozikRKWQpsCoMKmwquCrR"
        "KuYrCisuK0wrdCuVK6or0CvxLAssICxELGgshiyuLNAs5S0MLS4tLi0uLTktTS1eLXgtjy2hLbQt1C30LgAuDC4oLkcucC5/"
        "Lo4umS6kLrAuvC7ILtQu8C8MLzIvWC9tL4Ivli+3L9kv+zAPMCMwdTDHMPMxHzFBMVUxcTGyMe8yCzIvMl4yhTK+Mtgy5TL6"
        "MzYzWzOaM8oz+DQeNDk0XzSnNMk09TURNSU1MTVKNWI1dTWPNbs15zYZNkw2ZjZxNoU2lDaoNtY28DcQNyM3STdgN6A36Df5"
        "OAU4GTgtOE44Yjh2OIo4mjisOMA4zTjhOPU5ETktOT45TzlbOWM5dzmYOaA5qDm4Oco50jnfOfs6FwAAAAIAZP9xAlkCnQAD"
        "AAcAABcRIRElIREhZAH1/nABK/7VjwMs/NRlAmIAAAIAPP/0Ag0CdwAPABcAABcRMzUzNTMVMxUzESM1IRURITUjNSMVIzxb"
        "WmZaXGb++gEGWlJaDAHOWltbWv4ytbUBGqpaWv//ADz/9AINA1ECJgABAAAABwIlAIAAtP//ADz/9AINAyUCJgABAAAABwIq"
        "AIwAtP//ADz/9AINAyUCJgABAAAABwIoAI8AtP//ADz/9AINAyUCJgABAAAABwIiAJMAtP//ADz/9AINA1ECJgABAAAABwIk"
        "AHMAtP//ADz/9AINAxACJgABAAAABwItAJIAtP//ADz/LAJTAncCJgABAAAABwIxAUf/9P//ADz/9AINAyUCJgABAAAABwIr"
        "AI0AtP//ADz/9AINAycCJgABAAAABwIsAIcAtAACADz/9AN4AncAHwAnAAAXETM1MzUzFTM1IRUzFSM1IRUzFSMVITUzFSMV"
        "ITUhFREhNSM1IxUjPFtaZqwBGltl/vq1tQEGZVv+iv76AQZaUloMAc5aW1tbW2Raq2OsW2VatbUBGqpaWgD//wA8//QDeANF"
        "AiYACwAAAAcCJQFPAKgAAgA8//QCDQJ3AA8AFwAAFzUjETM1IRUzFSMVMxUjFSUhNSM1MzUhl1tbARpcXFxc/vABBra2/voM"
        "WgHOW1u+UMBaZKxjqwAAAQA8//QCDQJ3ABMAABc1IxEzNSEVMxUjNSERITUzFSMVl1tbARpcZv76AQZmXAxaAc5bW760/ka2"
        "wFr//wA8//QCDQNSAiYADgAAAAcCJQCAALX//wA8//QCDQMmAiYADgAAAAcCKQCPALX//wA8/zgCDQJ3AiYADgAAAAcCMACQ"
        "AAD//wA8//QCDQMmAiYADgAAAAcCKACPALX//wA8//QCDQMmAiYADgAAAAcCIwCPALUAAgA8//QCDQJ3AAsAEwAAFxEhFTMV"
        "MxEjFSMVJzM1MxEjNSM8ARtaXFxatqxaWqwMAoNbWv7nW1pkWwEFWv//ADz/9AINAyYCJgAUAAAABwIpAGQAtf//ABb/9AIN"
        "AncCBgAXAAAAAgAW//QCDQJ3AA8AGwAAFzUjNTMRIRUzFTMRIxUjFSczNTMRIzUjFTMVIzwmJgEbWlxcWrasWlqsXV0M/WUB"
        "IVta/udbWmRbAQVavGUAAQA8//QCDQJ3ABcAABc1IxEzNSEVMxUjNSEVMxUjFSE1MxUjFZdbWwEaXGb++ra2AQZmXAxaAc5b"
        "W2Raq2OsW2VaAP//ADz/9AINA1ICJgAYAAAABwIlAIAAtf//ADz/9AINAyYCJgAYAAAABwIqAIwAtf//ADz/9AINAyYCJgAY"
        "AAAABwIpAI8Atf//ADz/9AINAyYCJgAYAAAABwIoAI8Atf//ADz/9AINAyYCJgAYAAAABwIiAJMAtf//ADz/9AINAyYCJgAY"
        "AAAABwIjAI8Atf//ADz/9AINA1ICJgAYAAAABwIkAHMAtf//ADz/9AINAxECJgAYAAAABwItAJIAtf//ADz/LAINAncCJgAY"
        "AAAABwIxAJD/9AABADz/9AINAncADQAAFxEhFTMVIzUhFTMVIxE8AXVcZv76trYMAoNbZFqrY/7wAAABADz/9AINAncAGQAA"
        "FzUjETM1IRUzFSM1IREhNSM1IzUzFTMVIxWXW1sBGlxm/voBBlpcZrZcDFoBzltbvrT+RlFbY1nAWv//ADz/9AINAyYCJgAj"
        "AAAABwIqAIwAtf//ADz/9AINAyYCJgAjAAAABwIoAI8Atf//ADz/DAINAncCJgAjAAAABwIvAJMAAP//ADz/9AINAyYCJgAj"
        "AAAABwIjAI8AtQABADz/9AINAncADwAAFxEzETM1MxUzETMRIxEhETxlUGZQZmb++gwCg/7wW1sBEP19ARD+8AACADz/9AIN"
        "AncACwATAAAXETMVITUzESMRIRERMzUzFTM1ITxlAQZmZv76UGZQ/voMAoMlJf19ARD+8AFzW1uHAP//ADz/9AINAyYCJgAo"
        "AAAABwIoAI8AtQABADz/9AFXAncACwAAFzUzESM1IRUjETMVPFtbARtcXAxkAbplZf5GZP//ADz/9AOhAncAJgArAAAABwA2"
        "AZQAAP//ADz/9AFXA1ICJgArAAAABwIlACUAtf//ADz/9AFXAyYCJgArAAAABwIqADEAtf//ADz/9AFXAyYCJgArAAAABwIo"
        "ADQAtf//ADH/9AFrAyYCJgArAAAABwIiADgAtf//ADz/9AFXAyYCJgArAAAABwIjADQAtf//ADz/9AFXA1ICJgArAAAABwIk"
        "ABgAtf//ADz/9AFaAxECJgArAAAABwItADcAtf//ADz/LAFXAncCJgArAAAABgIxMfT//wA8//QBVwMoAiYAKwAAAAcCLAAs"
        "ALUAAQA8//QCDQJ3AA8AABc1IzUzFSERITUhFTMRIxWXW2UBBv7wARpcXAxawLYBumVb/jJa//8APP/0Ag0DJgImADYAAAAH"
        "AigAjwC1AAEAPP/0Ag0CdwAjAAAXETM1MxUjFTM1MzUzNTMVIxUjFSMVMxUzFTMVIzUjNSM1IxE8W2RaUFxaZlxaXFxaXGZa"
        "XFAMAihbZatbWltlWlpQW1tkWltb/vAA//8APP8MAg0CdwImADgAAAAHAi8AkwAAAAEAPP/0Ag0CdwALAAAXNSMRMxEhNTMV"
        "IxWXW2UBBmZcDFoCKf3hW2Va//8APP/0Ag0DUgImADoAAAAHAiUAgAC1//8APP/0Ag0CdwImADoAAAAHAicA6P/Y//8APP8M"
        "Ag0CdwImADoAAAAHAi8AkwAA//8APP/0Ag0CdwImADoAAAAHAd0AtwGgAAH/6P/0Ag8CdwATAAAXNSM1Byc3ETMVNxcHFSE1"
        "MxUjFZlbHzdWZT82dQEGZlwMWnQqKnEBRL1TKJzxW2VaAAABADz/9ALCAncAGwAAFxEzNTMVMxUzNTM1MxUzESMRIxUjESMR"
        "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"
        "NTM1IZdbWwEaXFxcXP7wAQa2tv76DFoBzltbvlDAWmSsY6sAAAEAPP/0AbECdQAFAAAXESEVIRE8AXX+8AwCgWX95AAAAQA8"
        "//QBsQJ1AAUAABcRIRUhETwBdf7wDAKBZf3kAAABADz/9AGxAr0ABwAAFxEhNTMVIRE8ARFk/vAMAoFIrf3kAAACADz/9AJn"
        "AnUACQANAAAXETMRIREjNSEVNyERITxaAdFl/p9aAQf++QwBGwFm/X9bW8ABXAABADz/9AINAncAFwAAFzUjETM1IRUzFSM1"
        "IRUzFSMVITUzFSMVl1tbARpcZv76trYBBmZcDFoBzltbZFqrY6xbZVoAAAMAPP/0Ag0DCQAXABsAHwAAFzUjETM1IRUzFSM1"
        "IRUzFSMVITUzFSMVATUzFTM1MxWXW1sBGlxm/vq2tgEGZlz+9TeQNgxaAc5bW2Raq2OsW2VaArBlZWVlAAEAPP/0A3gCdwA7"
        "AAAXNTM1MzUzNSM1IzUjNTMVMxUzFTMRMxEzNTM1MzUzFSMVIxUjFTMVMxUzFSM1IzUjNSMRIxEjFSMVIxU8W1pcXFpbZVpc"
        "UGZQXFplW1tbW1tbZVpcUGZQXFoMZVpbUFpaZVtaWwEQ/vBbWltlWlpQW1plW1pb/vABEFtaWwAAAQA8//QCDQJ3ABsAABc1"
        "IzUzFSE1ITUhNSEVIzUzNSEVMxUjFTMVIxWXW2UBBv7wARD++mVbARpcXFxcDFplW6xjq1pkW1u+UMBaAAEAPP/0Ag0CdwAT"
        "AAAXETMRMxEzNTM1MxEjESMRIxUjFTxlUFxaZmZQXFoMAoP+PAEPWlv9fQHE/vFbWgAAAgA8//QCDQMYABMAGwAAFxEzETMR"
        "MzUzNTMRIxEjESMVIxUTNTMVMzUzFTxlUFxaZmZQXFoIN4Y2DAKD/jwBD1pb/X0BxP7xW1oCv2UuLmUAAAIAPP/0Ag0DRQAj"
        "AC8AABcRMzUzFSMVMzUzNTM1MxUjFSMVIxUzFTMVMxUjNSM1IzUjERM1MzUzNTMVIxUjFTxbZFpQXFpmXFpcXFpcZlpcUGYt"
        "LTYsLQwCKFtlq1taW2VaWlBbW2RaW1v+8AK/Ny4tNy0uAAABADz/9AINAncAIwAAFxEzNTMVIxUzNTM1MzUzFSMVIxUjFTMV"
        "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"
        "l1tlAQb+8FtbARpcXP7wAQb++gxaZVusWr5bW/4yWgFzqwADADz/8wL0AncAEwAfADsAABc1MzUzNTM1MzUzFSMVIxUjFSMV"
        "AxEjFSM1MzUzFTMRATUjNTM1MzUjFSM1MzUzFTMVIxUjFTM1MxUjFadPUE9RWU9RT1BnJTguOS8BXi8vjII5L5YuLoyCOC4M"
        "qJ5Pn0+oUJ5PngE0ARcvOC8v/uD+yy6ZL1MvOC8vZi+FYWsuAAMAPP/zAzQCdwATAB8AOwAAFzUzNTM1MzUzNTMVIxUjFSMV"
        "IxUDESMVIzUzNTMVMxEBNSM1MxUzNSM1MzUjFSM1MzUzFTMVIxUzFSMV2U9QT1FZT1FPUJklOC45LwGeMDqAioqAOjCUMDAw"
        "MAyonk+fT6hQnk+eATQBFy84Ly/+4P7LLzguUzhSLjgvL2YkZy8AAwA8//MDUgJ3ABMALwBLAAAFNTM1MzUzNTM1MxUjFSMV"
        "IxUjFQM1IzUzNTM1IxUjNTM1MxUzFSMVIxUzNTMVIxUBNSM1MxUzNSM1MzUjFSM1MzUzFTMVIxUzFSMVAQpQT1BQWlBQUE/6"
        "Li6Mgjguli8vjII5LwGOMDqAioqAOjCUMDAwMAyonk+fT6hQnk+eATIuZy9TLzkuLmcvUy85Lv7NLzguUzhSLjgvL2YkZy8A"
        "AAQAPP/zAzACdwATAB8AKwAzAAAXNTM1MzUzNTM1MxUjFSMVIxUjFQMRIxUjNTM1MxUzEQE1IzUzNTM1MzUzESczNSMVIxUj"
        "2U9QT1FZT1FPUJklOC45LwIluy8vLmi7giUuLwyonk+fT6hQnk+eATQBFy84Ly/+4P7LXWcuLi/+sZWCLy4ABAA8//MDfwJ3"
        "ABMALwA7AEMAAAU1MzUzNTM1MzUzFSMVIxUjFSMVATUjNTMVMzUjNTM1IxUjNTM1MxUzFSMVMxUjFQE1IzUzNTM1MzUzEScz"
        "NSMVIxUjAShQT1BQWlBQUE/+6S85gIqKgDkvlDAwMDACSLsvLi9nu4MlLy8MqJ5Pn0+oUJ5PngE0LzguUzhSLjgvL2YkZy/+"
        "y11nLi4v/rGVgi8uAAUAPP/zAysCdwATAB8AMwA3ADsAABc1MzUzNTM1MzUzFSMVIxUjFSMVAxEjFSM1MzUzFTMRATUjNTM1"
        "IzUzNTMVMxUjFTMVIxUnMzUjNTM1I9lPUE9RWU9RT1CZJTguOS8Bky8vLy+WMDAwMIyCgoKCDKieT59PqFCeT54BNAEXLzgv"
        "L/7g/ssvZyRnLy9nJGcvOVM4VAAABQA8//MDcAJ3ABMALwAzAEcASwAABTUzNTM1MzUzNTMVIxUjFSMVIxUBNSM1MxUzNSM1"
        "MzUjFSM1MzUzFTMVIxUzFSMVBTM1Iwc1IzUzNSM1MzUzFTMVIxUzFSMVJzM1IwEeUE9QUFpQUFBP/vMvOYCKioA5L5QwMDAw"
        "AbaCggovLy8vli8vLy+MgoIMqJ5Pn0+oUJ5PngE0LzguUzhSLjgvL2YkZy/8U4wvZyRnLy9nJGcvxFQAAAUAPP/zA44CdwAT"
        "ADMANwBLAE8AAAU1MzUzNTM1MzUzFSMVIxUjFSMVATUjNTMVMzUjNSM1IzUzNTMVMxUjNSMVMxUzFTMVIxUFMzUjBzUjNTM1"
        "IzUzNTMVMxUjFTMVIxUnMzUjATxPUE9RWU9RT1D+1S44gl0vLi6WMDqCL10wMAHSg4MKLi4uLpYwMDAwjIODDKieT59PqFCe"
        "T54BMy84LiQvLmcvL2deVC4vOC/7U4wvZyRnLy9nJGcvxFQAAAUAPP/zA6ICdwATAB8AMwA3ADsAAAU1MzUzNTM1MzUzFSMV"
        "IxUjFSMVAxEjFSM1MzUzFTMRATUjNTM1IzUzNTMVMxUjFTMVIxUnMzUjNTM1IwFQT1BPUVlPUU9Qs4I4LpYwAawuLi4uljAw"
        "MDCMgoKCggyonk+fT6hQnk+eATMBGC84Ly/+3/7ML2ckZy8vZyRnLzlTOFQAAgA8//MBLwFCAAsADwAAFzUjNTM1MxUzFSMV"
        "JzM1I2ouLpYvL4yCgg0v8S8v8S853gABADz/8wDSAUIACwAAFxEjFSM1MzUzFTMRmSU4LjkvDQEXLzgvL/7gAAABADz/8wEv"
        "AUIAGwAAFzUjNTM1MzUjFSM1MzUzFTMVIxUjFTM1MxUjFWouLoyCOC6WLy+MgjkvDS5nL1MvOC8vZi9TLzkuAAABADz/8wEv"
        "AUIAGwAAFzUjNTMVMzUjNTM1IxUjNTM1MxUzFSMVMxUjFWsvOYCKioA5L5QwMDAwDS84LlM4Ui44Ly9mJGcvAAACADz/8wEw"
        "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"
        "MzUzNTM1MzUzFSMVIxUjFSMVMxUzFTMVMxUCx1pbW1tbW1taZlxaW1tbW1pc/l1bW1tbW1tbW2VcWltbW1taXGtbW1pbY1tb"
        "WltlWltaUFtaW2VbW1pbY1tbWltlWltaUFtaW2UAAAIACv+VAxkCzgAjAEcAAAU1MzUzNTM1MzUjNSM1IzUjNTMVMxUzFTMV"
        "MxUjFSMVIxUjFSE1MzUzNTM1MzUjNSM1IzUjNTMVMxUzFTMVMxUjFSMVIxUjFQFIW1tbW1tbW1tlW1tbW1tbW1v+XVtbW1tb"
        "W1tbZVtbW1tbW1tba2VbWltQWltaZVtaW1tjW1pbW2VbWltQWltaZVtaW1tjW1pbWwAAAQA8/5UCDQLOACMAAAU1IzUjNSM1"
        "IzUzNTM1MzUzNTMVIxUjFSMVIxUzFTMVMxUzFQGnWlxaW1taXFpmXFpcWlpcWlxrW1taW2NbW1pbZVpbWlBbWltlAAEAHv+V"
        "Ae8CzgAjAAAXNTM1MzUzNTM1IzUjNSM1IzUzFTMVMxUzFTMVIxUjFSMVIxUeW1tbW1tbW1tlW1taXFxaW1trZVtaW1BaW1pl"
        "W1pbW2NbWltbAAACADwBuAGUAncACwAXAAABNTM1IzUzFTMVIxUjNTM1IzUzFTMVIxUBMC5cZiws/y5bZS0tAbg4ImVbNi44"
        "ImVbNi4AAAEAPAG4AM4CdwALAAATNTM1IzUzFTMVIxVpLltlLS0BuDgiZVs2LgAB/+D/mQINAncAEQAABzUzETM1IRUzFSM1"
        "IRUzFSMRIFxbARpcZv76trZnZQIeW1tkWqtj/pUAAAIAPAA+Ag0B3wAzADcAADc1IzUjNTM1MzUzFTMVMxUzFSMVIxUjNSMV"
        "IzUjNTM1MxUzFTM1MzUjNSM1IxUjFTMVMxUnMzUjly4tLS7tLS4uLi40H4oqKoopIC4uLdkuLrZ5dnY+Li3sLC4uLC1lLS0m"
        "JiZ6JydxNVEtLS3YLTiKZwADADz/9AJoAncAHwAnAC8AABc1IzUzNTM1IzUjNTM1IRUzFSMVIxUzFTMVMxUjNSMVJSE1IzUj"
        "FSM3MzUzNSEVM5dbW1paW1sBGlxcWlpcW2VS/vABBlpSWlpSWv76WgxaZVtQWmRbW2RaUFtbZFpaZFFbW75bUFAAAAEAPP8/"
        "AsECdwAPAAAFESE1IxEzNSEVIxEjESMRAaf+8FtbAioyNIDBAWxaARdbZf0tAtP9LQAAAQA8//QCDQJ3ABkAABc1IzUzNSM1"
        "MzUhFTMVIzUhFSEVMxUhFSEVl1tbW1sBGlxm/voBEFz+lAFsDFrAUL5bW2Raq1llUWQAAwBGAWIBUgJtAAsADwAjAAATNSM1"
        "MzUzFTMVIxUnMzUjFzUjNTM1MxUzFSM1IxUzNTMVIxVzLS2xLi6nnZ0yEhI+ERsqKhsRAWIusC0tsC44nIARPhMTGhAqEx0R"
        "AAMARgFiAVICbQALAA8AGwAAEzUjNTM1MxUzFSMVJzM1Ixc1MzUzFTMVIzUjFXMtLbEuLqednS4MJgwNIwFiLrAtLbAuOJxz"
        "MQwMDQwwAAIAPAEpAtgCdwAbACsAAAERMzUzFTMVMzUzNTMVMxEjESMVIxUjNSM1IxEhESMVIzUzNTMVMxUjNSMRAYkvOC4l"
        "LjotNyYuOS4k/tclOC6WLjglASkBHy8vW1svL/7hARZci4tc/uoBFi84Ly84L/7qAAIAPAGIAPwCSAALAA8AABM1IzUzNTMV"
        "MxUjFSczNSNgJCR4JCRuZGQBiCV2JSV2JS5jAAABADz/9AChAncAAwAAFxEzETxlDAKD/X0AAAEAPP/0AVYCdwALAAAXESM1"
        "MzUzFTMVIxGWWlplW1sMAZ9lf39l/mEAAAQAPP/zBEsCdwATABcAIwAnAAAXETMVMxUzETMRMxEjNSM1IxEjEQU1IRUlNSMR"
        "MzUhFTMRIxUlIREhPGVaXFBmZlpcUAIgAUL+0ltbARtbW/7vAQf++QwCg1ta/vEBxP19WlsBD/48AWVltFoBGVtb/udaZAEF"
        "AAEAPAAAAg0CkwAbAAAzNSM1IxEzNTM1MxUzFTMVIzUhESE1MxUjFSMV7ldbW1dtVlxm/voBBmZcVmJaARlbY2NbZFr++1pk"
        "WmIAAwA8/7kCDQKyACMAJwArAAAXNyM1IxEzNTM3MwczNzMHMxUzFSM1IwMzNTMVIxUjByM3IwcnMxMjEzMTI6IJFFtbcwk0"
        "CSYJMwkaXGYfQWBmXHkJNAklCTUZQFlNJUEmRztaAc5bOzs7O1u+tP5GtsBaOzs7nwG6/kYBugAABgA8AIwBigHYAAsADwAT"
        "ABcAGwAfAAA3NSM1MzUzFTMVIxUHNTMVAzUzFRczNSMXNTMVAzUzFZguLpUuLvE4ODgugYGvOTk5ui6ULi6ULi44OAEUODiu"
        "gOY4OAEUODgAAQA8/5kCDQLSACMAABc1IzUjNTMVITUhNSM1MzUzNTMVMxUzFSM1IRUhFTMVIxUjFfpjW2UBBv7wW1tjZlFc"
        "Zv76ARBcXFFnW1plW6xavltbW1tkWqtZwFpbAAABADz/9AINAncAGwAAFzUjETM1IRUzFSM1IRUzFSMVMxUjFSE1MxUjFZdb"
        "WwEaXGb++re3t7cBBmZcDFoBzltbvrRmZTFkWrbAWgAAAQAW//QCDQJ3ABEAABc1MxEhFTMVIzUhFTMVIxUzFRYmAXVcZv76"
        "trYsDGUCHltkWqtjq2UAAQA8//QCZwJ3ABsAABc1MxEzNSEVMxUjNSEVMxUjFTMVIxUhNTMVIxU8W1oBHFpk/vi2tra2AQhk"
        "WgxkAcRbW2RaeWQ6ZD9bZVoAAAQAPP/0BG4CdwALAA8AIwA3AAAXETM1IRUzESMVIRURIREhATUjNSM1MzUzFTMVIxUzNTMV"
        "IxUzNTM1IzUjNTM1MxUjFTMVMxUjFTxbARpcXP7wAQb++gH3LS0tN4aGfDctXLOFLi68soUtLQwCKFtb/uhbtQEaAQT94i2E"
        "N1hYN3osNi03Iiw2LTciLDYtAAEAPP/0AmcCdwAXAAAXNTMRMzUhFTMVIzUhFTMVIxUhNTMVIxU8W1oBHFpk/vi2tgEIZFoM"
        "ZAHEW1tkWqtjrFtlWgAAAQA8//QCDQJ3ACMAABc1IzUzNSM1MzUjNSM1MxUzFTM1MzUzFSMVIxUzFSMVMxUjFfFaWlpaWltl"
        "WlJaZlxaWlpaWgxSZCNlLFq/tVtbtb9aLGUjZFIAAAEAPP/0AdQCdwATAAAXNTM1MzUzNTM1MxUjFSMVIxUjFTxPUE9RWU9R"
        "T1AMqJ5Pn0+oUJ5PngAAAQBDAFcCFAIlAAsAADc1IzUzNTMVMxUjFfi1tWa2tleqZMDAZKoAAAEAbQDxAeoBVgADAAA3NSEV"
        "bQF98WVlAAEAYABwAfcCBAALAAA3JzcnNxc3FwcXByeuR4GIR4iBR4B4R3lwR3+HR4eAR4B3R3cAAwBtAFEB6gH7AAMABwAL"
        "AAA3NSEVBzUzFQM1MxVtAX3xZWVl8WVloGVlAUZkZAAAAgBtAIcB6wGHAAMABwAAEzUhFQU1IRVtAX7+ggF+ASJlZZtkZAAD"
        "AG0ACgHrAf8AAwAHAAsAADcTMwMnNSEVJTUhFaqVZZWiAX7+ggF+CgH1/gt9ZGSbZWUAAQBD/5UCFALOACMAABc1MzUzNTM1"
        "MzUjNSM1IzUjNTMVMxUzFTMVMxUjFSMVIxUjFUNbW1tbW1tbW2VbW1pcXFpbW2tlW1pbUFpbWmVbWltbY1taW1sAAAEAQ/+V"
        "AhQCzgAjAAAFNSM1IzUjNSM1MzUzNTM1MzUzFSMVIxUjFSMVMxUzFTMVMxUBrlpcWltbWlxaZlxaXFpaXFpca1tbWltjW1ta"
        "W2VaW1pQW1pbZQACAH7/xAHaAs4AIwAnAAA3NTM1MzUzNTM1IzUjNSM1IzUzFTMVMxUzFTMVIxUjFSMVIxUHNSEVf0RDREJC"
        "RENETkNDQ0REQ0NDTwFcak1DQkM5Q0RCTUNDQ0NNQ0JDQ6ZAQAAAAgB+/8QB2QLOACMAJwAAJTUjNSM1IzUjNTM1MzUzNTM1"
        "MxUjFSMVIxUjFTMVMxUzFTMVBTUhFQGKQ0NDQ0NDQ0NORENDQ0NDQ0T+pgFbakNDQkNNQ0NDQ01CREM5Q0JDTaZAQAAAAgBl"
        "//kB9AHrAAsADwAANzUjNTM1MxUzFSMVBzUhFfaRkWySkv0Bj5t4YHh4YHiiZGQA//8AsACEAacBiQImAhcAXQAGAhcAvwAB"
        "ALAAxQGnASwACwAANzUzFTM1MxUjNSMVsJglOpklxWcuLmcvLwAAAQBtAMcB6gGHAAUAACU1ITUhFQGF/ugBfcdbZcAAAQA8"
        "AhIAzgJ3AAsAABM1MzUzFTMVIzUjFTwtOC03JAISNy4uNy4uAAMAPP/0A3gBwgATABcAGwAAFzUjETM1IRUzNSEVMxEjFSE1"
        "IxUlIREhASERIZdbWwEaUgEaW1v+5lL+8AEG/voBbAEG/voMWgEZW1tbW/7nWlpaZAEG/voBBgAAAQAA//MCSAJ2AA8AABU1"
        "MxEzNSEVMxEzFSMRIRE8WwEaXDuh/voNZQHEWlr+PGUCH/3hAAIAPP/0Ag0CdwALABMAABcRMzUzNTMVMxUzESUhESM1IxUj"
        "PFtaZlpc/pQBBlpSWgwBzlpbW1r+MmUBX1paAAABADz/9AINAncABwAAFxEhESMRIRE8AdFl/vkMAoP9fQIe/eIAAAEAO//0"
        "AsECdwAbAAAXNSM1MzUzNSM1IzUzNSEVIRUzFSEVIRUjFSEVlltbtrZbWwIr/d+2ARH+77YCIQxbZFtQWmVaZFFbY1tQZQAB"
        "ADz/RAINAcIACwAAFxEzESERMxEjFSEVPGUBBmZc/vC8An7+lgFq/oxasAAAAwA8//QDJgJ3ABsAJwAzAAAXNTM1MzUzNTM1"
        "MzUzNTMVIxUjFSMVIxUjFSMVAzUjNTM1MxUzFSMVATUjNTM1MxUzFSMVbVxaXFpbXGRaXFtaXFo8W1tkXFwBa1tbZVtbDGRb"
        "W1lbWltlWlpaW1taAWpaZFtbZFr+llplW1tlWgAHAB7/wgJFAqgACwAXABsAJwArAC8AMwAAEzUjNTM1MxUzFSMVAzUjNTM1"
        "MxUzFSMVJzM1IwU1IzUzNTMVMxUjFSczNSMlNSEVJTM1I2ouLpYvL5YuLpYvL4yCggENLy+WLi6Ng4P+lAH5/l2CggFaLvIu"
        "LvIu/mguhi4uhi44cqouhi4uhi44costLZveAAAC//kCDAEzAnEAAwAHAAADNTMVMzUzFQdmb2UCDGVlZWUAAQBjAgwAyQJx"
        "AAMAABM1MxVjZgIMZWUAAQA8AgwAzgKdAAsAABM1IzUjNTMVMxUzFZcuLTcuLQIMLC43Li02AAEATQIMAN8CnQALAAATNTM1"
        "MzUzFSMVIxVNLS43LS4CDDYtLjcuLAACABICDAEbAp0ACwAXAAATNSM1IzUzFTMVMxUjNSM1IzUzFTMVMxXkLi03Li2uLi03"
        "Li0CDCwuNy4tNiwuNy4tNgABAGEB4ADzAp8ACwAAEzUzNSM1MxUzFSMVji5bZS0tAeA4ImVbNi4AAQBNAgwA3wJxAAsAABM1"
        "MzUzFTMVIzUjFU0tOC03JAIMNy4uNy4uAAEATQIMAN8CcQALAAATNSM1MxUzNTMVIxV6LTckNy0CDC43Li43LgABAB0CDAEQ"
        "AnEABwAAEzUzFTM1MxUdN4Y2AgxlLi5lAAIAZAIMAMkCcQADAAcAABM1MxUnMzUjZGVEIyMCDGVlISMAAAEAGwIMARICcwAL"
        "AAATNTMVMzUzFSM1IxUbmCU6mSUCDGcuLmcvLwABAAkCDAEjAlwAAwAAEzUhFQkBGgIMUFAAAAEATQIMAN8CywALAAATNSM1"
        "MzUzFSMVMxV6LS04LlsCDFs2LjgiZQABAFj/DADq/8sACwAAFzUzNSM1MxUzFSMVhS5bZS0t9DgiZVs2LgAAAQAf/zgBDAAl"
        "ABMAABc1IzUzFTM1IzUjNTMVMxUzFSMVTS44fi4tNy4tLcgtNy0jLWZcLTctAAABAB//OAEMACYAEwAAFzUjNTM1MzUzFSMV"
        "IxUzNTMVIxVNLi4tNy0tfjctyC03LV1nLSMtNy0AAAIAEwIMARECcQADAAcAABM1MxUzNTMVE2YzZQIMZWVlZQAC//cB4wEx"
        "AkgAAwAHAAADNTMVMzUzFQlmb2UB42VlZWUAAQBsAh0A0gKCAAMAABM1MxVsZgIdZWX//wA8AgwAzgKdAAYCJAAAAAEAPAIc"
        "AM4CrQALAAATNTM1MzUzFSMVIxU8LS43LS4CHDYtLjcuLAACAAwCHQEVAq4ACwAXAAATNSM1IzUzFTMVMxUjNSM1IzUzFTMV"
        "MxXeLi03Li2uLi03Li0CHSwuNy4tNiwuNy4tNv//AE0CDADfAnEABgIoAAD//wBNAgwA3wJxAAYCKQAAAAEAGAIcAQsCgQAH"
        "AAATNTMVMzUzFRg3hjYCHGUuLmUAAgBeAh0AwwKCAAMABwAAEzUzFSczNSNeZUQjIwIdZWUhIwD//wAbAgwBEgJzAAYCLAAA"
        "AAEADQIdAScCggADAAATNSEVDQEaAh1lZQAAAQAs/zgBGQAlABMAABc1IzUzFTM1IzUjNTMVMxUzFSMVWi44fi4tNy4tLcgt"
        "Ny0jLWZcLTctAAABACD/OAENACYAEwAAFzUjNTM1MzUzFSMVIxUzNTMVIxVOLi4tNy0tfjctyC03LV1nLSMtNy0AAAAAAAAZ"
        "ATIAAQAAAAABAAAGAAAAAQAAAAABAQAHAAYAAQAAAAABAgAGAA0AAQAAAAABAwAIABMAAQAAAAABBAAEABsAAwABBAkAAAC0"
        "AB8AAwABBAkAAQAaANMAAwABBAkAAgAOAO0AAwABBAkAAwA+APsAAwABBAkABAAqATkAAwABBAkABQAaAWMAAwABBAkABgAo"
        "AX0AAwABBAkABwCaAaUAAwABBAkACAAsAj8AAwABBAkACQAiAmsAAwABBAkACwCEAo0AAwABBAkADAA+AxEAAwABBAkADQEi"
        "A08AAwABBAkADgA2BHEAAwABBAkAGQAYBKcAAwABBAkBAAAMBL8AAwABBAkBAQAOAO0AAwABBAkBAgAMBMsAAwABBAkBAwAQ"
        "BNcAAwABBAkBBAAIBOdXZWlnaHRSZWd1bGFyTWVkaXVtU2VtaUJvbGRCb2xkAEMAbwBwAHkAcgBpAGcAaAB0ACAAMgAwADIA"
        "MQAgAFQAaABlACAAUABpAHgAZQBsAGkAZgB5ACAAUwBhAG4AcwAgAFAAcgBvAGoAZQBjAHQAIABBAHUAdABoAG8AcgBzACAA"
        "KABoAHQAdABwAHMAOgAvAC8AZwBpAHQAaAB1AGIALgBjAG8AbQAvAGUAaQBmAGUAdAB4AC8AUABpAHgAZQBsAGkAZgB5AC0A"
        "UwBhAG4AcwApAFAAaQB4AGUAbABpAGYAeQAgAFMAYQBuAHMAUgBlAGcAdQBsAGEAcgAxAC4AMAAwADAAOwBOAE8ATgBFADsA"
        "UABpAHgAZQBsAGkAZgB5AFMAYQBuAHMALQBSAGUAZwB1AGwAYQByAFAAaQB4AGUAbABpAGYAeQAgAFMAYQBuAHMAIABSAGUA"
        "ZwB1AGwAYQByAFYAZQByAHMAaQBvAG4AIAAxAC4AMAAwADAAUABpAHgAZQBsAGkAZgB5AFMAYQBuAHMALQBSAGUAZwB1AGwA"
        "YQByAFAAaQB4AGUAbABpAGYAeQAgAFMAYQBuAHMAIABpAHMAIABhACAAdAByAGEAZABlAG0AYQByAGsAIABvAGYAIABTAHQA"
        "ZQBmAGkAZQAgAEoAdQBzAHQAcAByAGkAbgBjAGUALgAoAGgAdAB0AHAAcwA6AC8ALwBnAGkAdABoAHUAYgAuAGMAbwBtAC8A"
        "ZQBpAGYAZQB0AHgAKQBUAHkAcABlAGMAYQBsAGkAcwBtACAARgBvAHUAbgBkAHIAeQBsAGkAbgBlAFMAdABlAGYAaQBlACAA"
        "SgB1AHMAdABwAHIAaQBuAGMAZQBoAHQAdABwAHMAOgAvAC8AdwB3AHcALgBtAHkAZgBvAG4AdABzAC4AYwBvAG0ALwBjAG8A"
        "bABsAGUAYwB0AGkAbwBuAHMALwB0AHkAcABlAGMAYQBsAGkAcwBtAC0AZgBvAHUAbgBkAHIAeQBsAGkAbgBlAC0AZgBvAHUA"
        "bgBkAHIAeQBoAHQAdABwAHMAOgAvAC8AZQBpAGYAZQB0AHMAdAB5AHAAZQAuAGcAdQBtAHIAbwBhAGQALgBjAG8AbQAvAFQA"
        "aABpAHMAIABGAG8AbgB0ACAAUwBvAGYAdAB3AGEAcgBlACAAaQBzACAAbABpAGMAZQBuAHMAZQBkACAAdQBuAGQAZQByACAA"
        "dABoAGUAIABTAEkATAAgAE8AcABlAG4AIABGAG8AbgB0ACAATABpAGMAZQBuAHMAZQAsACAAVgBlAHIAcwBpAG8AbgAgADEA"
        "LgAxAC4AIABUAGgAaQBzACAAbABpAGMAZQBuAHMAZQAgAGkAcwAgAGEAdgBhAGkAbABhAGIAbABlACAAdwBpAHQAaAAgAGEA"
        "IABGAEEAUQAgAGEAdAA6ACAAaAB0AHQAcABzADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBGAEwA"
        "aAB0AHQAcABzADoALwAvAHMAYwByAGkAcAB0AHMALgBzAGkAbAAuAG8AcgBnAC8ATwBGAEwAUABpAHgAZQBsAGkAZgB5AFMA"
        "YQBuAHMAVwBlAGkAZwBoAHQATQBlAGQAaQB1AG0AUwBlAG0AaQBCAG8AbABkAEIAbwBsAGQAAAAAAgAAAAAAAP+1ADIAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAJAAAAAJADJAQIAxwBiAK0BAwEEAGMArgCQAQUAJQAmAP0A/wBkAQYBBwAnAQgBCQDpACgAZQEK"
        "AQsAyADKAQwAywENAQ4AKQAqAPgBDwEQAREAKwESARMALAEUAMwBFQDNAM4A+gDPARYBFwEYAC0BGQAuARoALwEbARwBHQEe"
        "AOIAMAAxAR8BIAEhAGYBIgAyANABIwDRAGcA0wEkASUAkQEmAK8AsAAzAO0ANAA1AScBKAEpADYBKgDkAPsBKwEsAS0ANwEu"
        "AS8BMAExADgA1AEyANUAaADWATMBNAE1ATYBNwA5ADoBOAE5AToBOwA7ADwA6wE8ALsBPQA9AT4A5gE/AEQAaQFAAGsAbABq"
        "AUEBQgBuAG0AoAFDAEUARgD+AQAAbwFEAUUARwFGAQEA6gBIAHABRwFIAHIAcwFJAHEBSgFLAEkASgD5AUwBTQFOAEsBTwFQ"
        "AEwA1wB0AVEAdgB3AVIAdQFTAVQBVQFWAE0BVwFYAE4BWQBPAVoBWwFcAV0A4wBQAFEBXgFfAWABYQB4AWIAUgB5AWMAewB8"
        "AHoBZAFlAKEBZgB9ALEAUwDuAFQAVQFnAWgBaQBWAWoA5QD8AWsBbACJAFcBbQFuAW8BcABYAH4BcQCAAIEAfwFyAXMBdAF1"
        "AXYAWQBaAXcBeAF5AXoAWwBcAOwBewC6AXwAXQF9AOcBfgF/AYABgQDAAMEBggCdAJ4BgwGEAYUBhgGHAYgBiQGKAYsBjAGN"
        "AY4BjwGQAZEBkgGTAZQBlQGWAZcBmAGZAZoBmwGcAZ0BngGfAaABoQGiAaMBpAGlAaYBpwGoAakBqgGrAawBrQGuAa8BsAGx"
        "AbIBswG0AbUBtgG3AbgBuQG6AbsBvAG9Ab4BvwHAAcEBwgHDAcQBxQHGAccByAHJAcoBywHMAc0BzgHPAdAB0QHSAdMB1AHV"
        "AdYB1wHYAdkB2gHbAdwB3QHeAd8B4AHhAeIB4wHkAeUB5gHnAegB6QHqAesB7AHtAe4B7wHwAfEB8gHzAfQB9QH2AfcB+AH5"
        "AfoB+wH8Af0B/gH/AgACAQICAgMCBAIFAgYCBwIIAgkCCgCbAgsCDAINAg4CDwIQAhECEgITAhQCFQIWAhcCGAATABQAFQAW"
        "ABcAGAAZABoAGwAcAPQCGQIaAPUA9gIbAhwCHQIeAh8CIAIhAiICIwIkAiUCJgInAigCKQIqAisCLAItAi4CLwIwAjECMgAD"
        "AjMAEQAPAB0AHgCrAAQAowAiAKIAwwCHAA0CNAAGABIAPwI1AjYAEACyALMAQgALAAwAXgBgAD4AQADEAMUAtAC1ALYAtwCp"
        "AKoAvgC/AAUACgCmACMACQCIAIYAiwCKAIwAgwBfAIICNwCEAjgAvQAHAjkA9wI6AjsAhQCWAjwADgDvAPAAuAAgAI8AIQAf"
        "AJUAlACTAKcAYQCkAEEAkgI9Aj4AmgCZAj8ACADGAkACQQJCAkMCRAJFAkYCRwJIAkkCSgJLAkwCTQJOAk8CUACOANwAQwCN"
        "AN8A2ADhANsA3QDZANoA3gDgBkFicmV2ZQdBbWFjcm9uB0FvZ29uZWsHQUVhY3V0ZQtDY2lyY3VtZmxleApDZG90YWNjZW50"
        "BkRjYXJvbgZEY3JvYXQGRWJyZXZlBkVjYXJvbgpFZG90YWNjZW50B0VtYWNyb24HRW9nb25lawtHY2lyY3VtZmxleAd1bmkw"
        "MTIyCkdkb3RhY2NlbnQESGJhcgtIY2lyY3VtZmxleAJJSgZJYnJldmUHSW1hY3JvbgdJb2dvbmVrBkl0aWxkZQtKY2lyY3Vt"
        "ZmxleAd1bmkwMTM2BkxhY3V0ZQZMY2Fyb24HdW5pMDEzQgRMZG90Bk5hY3V0ZQZOY2Fyb24HdW5pMDE0NQNFbmcGT2JyZXZl"
        "DU9odW5nYXJ1bWxhdXQHT21hY3JvbgtPc2xhc2hhY3V0ZQZSYWN1dGUGUmNhcm9uB3VuaTAxNTYGU2FjdXRlC1NjaXJjdW1m"
        "bGV4B3VuaTAyMTgHdW5pMUU5RQRUYmFyBlRjYXJvbgd1bmkwMTYyB3VuaTAyMUEGVWJyZXZlDVVodW5nYXJ1bWxhdXQHVW1h"
        "Y3JvbgdVb2dvbmVrBVVyaW5nBlV0aWxkZQZXYWN1dGULV2NpcmN1bWZsZXgJV2RpZXJlc2lzBldncmF2ZQtZY2lyY3VtZmxl"
        "eAZZZ3JhdmUGWmFjdXRlClpkb3RhY2NlbnQGYWJyZXZlB2FtYWNyb24HYW9nb25lawdhZWFjdXRlC2NjaXJjdW1mbGV4CmNk"
        "b3RhY2NlbnQGZGNhcm9uBmVicmV2ZQZlY2Fyb24KZWRvdGFjY2VudAdlbWFjcm9uB2VvZ29uZWsLZ2NpcmN1bWZsZXgHdW5p"
        "MDEyMwpnZG90YWNjZW50BGhiYXILaGNpcmN1bWZsZXgGaWJyZXZlCWkubG9jbFRSSwdpbWFjcm9uB2lvZ29uZWsGaXRpbGRl"
        "AmlqB3VuaTAyMzcLamNpcmN1bWZsZXgHdW5pMDEzNwZsYWN1dGUGbGNhcm9uB3VuaTAxM0MEbGRvdAZuYWN1dGULbmFwb3N0"
        "cm9waGUGbmNhcm9uB3VuaTAxNDYDZW5nBm9icmV2ZQ1vaHVuZ2FydW1sYXV0B29tYWNyb24Lb3NsYXNoYWN1dGUGcmFjdXRl"
        "BnJjYXJvbgd1bmkwMTU3BnNhY3V0ZQtzY2lyY3VtZmxleAd1bmkwMjE5BHRiYXIGdGNhcm9uB3VuaTAxNjMHdW5pMDIxQgZ1"
        "YnJldmUNdWh1bmdhcnVtbGF1dAd1bWFjcm9uB3VvZ29uZWsFdXJpbmcGdXRpbGRlBndhY3V0ZQt3Y2lyY3VtZmxleAl3ZGll"
        "cmVzaXMGd2dyYXZlC3ljaXJjdW1mbGV4BnlncmF2ZQZ6YWN1dGUKemRvdGFjY2VudANmX2YFZl9mX2kFZl9mX2wDc190B3Vu"
        "aTA0MTAHdW5pMDQxMQd1bmkwNDEyB3VuaTA0MTMHdW5pMDQwMwpHZXVwdHVybmN5B3VuaTA0MTQHdW5pMDQxNQd1bmkwNDAx"
        "B3VuaTA0MTYHdW5pMDQxNwd1bmkwNDE4B3VuaTA0MTkHdW5pMDQxQQd1bmkwNDBDB3VuaTA0MUIHdW5pMDQxQwd1bmkwNDFE"
        "B3VuaTA0MjAHdW5pMDQyMQd1bmkwNDIyB3VuaTA0MjMHdW5pMDQwRQd1bmkwNDI0B3VuaTA0MjUHdW5pMDQyNwd1bmkwNDI2"
        "B3VuaTA0MjgHdW5pMDQyOQd1bmkwNDBGB3VuaTA0MkMHdW5pMDQyQgd1bmkwNDJBB3VuaTA0MDkHdW5pMDQwQQd1bmkwNDA1"
        "B3VuaTA0MDQHdW5pMDQyRAd1bmkwNDA3B3VuaTA0MDgHdW5pMDQwQgRZdWN5BFlhY3kHdW5pMDQwMgd1bmkwNDMwB3VuaTA0"
        "MzEHdW5pMDQzMgd1bmkwNDMzB3VuaTA0NTMKZ2V1cHR1cm5jeQd1bmkwNDM0B3VuaTA0MzUHdW5pMDQ1MQd1bmkwNDM2B3Vu"
        "aTA0MzcHdW5pMDQzOAd1bmkwNDM5B3VuaTA0M0EHdW5pMDQ1Qwd1bmkwNDNCB3VuaTA0M0MHdW5pMDQzRAd1bmkwNDNFB3Vu"
        "aTA0M0YHdW5pMDQ0MAd1bmkwNDQxB3VuaTA0NDIHdW5pMDQ0Mwd1bmkwNDVFB3VuaTA0NDQHdW5pMDQ0NQd1bmkwNDQ3B3Vu"
        "aTA0NDYHdW5pMDQ0OAd1bmkwNDQ5B3VuaTA0NUYHdW5pMDQ0Qwd1bmkwNDRCB3VuaTA0NEEHdW5pMDQ1OQd1bmkwNDVBB3Vu"
        "aTA0NTUHdW5pMDQ1NAd1bmkwNDREB3VuaTA0NTYHdW5pMDQ1Nwd1bmkwNDU4B3VuaTA0NUIEeXVjeQR5YWN5B3VuaTA0NTIF"
        "QWxwaGEEQmV0YQVHYW1tYQdFcHNpbG9uBFpldGEDRXRhBVRoZXRhBElvdGEFS2FwcGEGTGFtYmRhAk11Ak51AlhpB09taWNy"
        "b24CUGkDUmhvBVNpZ21hA1RhdQdVcHNpbG9uA1BoaQNDaGkDUHNpCkFscGhhdG9ub3MMRXBzaWxvbnRvbm9zCEV0YXRvbm9z"
        "CUlvdGF0b25vcwxPbWljcm9udG9ub3MMVXBzaWxvbnRvbm9zCk9tZWdhdG9ub3MMSW90YWRpZXJlc2lzD1Vwc2lsb25kaWVy"
        "ZXNpcwVhbHBoYQRiZXRhBWdhbW1hBWRlbHRhB2Vwc2lsb24EemV0YQNldGEFdGhldGEEaW90YQVrYXBwYQZsYW1iZGECbnUC"
        "eGkHb21pY3JvbgNyaG8Fc2lnbWEDdGF1B3Vwc2lsb24DcGhpA2NoaQNwc2kFb21lZ2EMdXBzaWxvbnRvbm9zDG9taWNyb250"
        "b25vcwpvbWVnYXRvbm9zCmFscGhhdG9ub3MMZXBzaWxvbnRvbm9zCGV0YXRvbm9zB3VuaTIxNTMHdW5pMjE1NAlvbmVlaWdo"
        "dGgMdGhyZWVlaWdodGhzC2ZpdmVlaWdodGhzDHNldmVuZWlnaHRocwd1bmkyMDgwB3VuaTIwODEHdW5pMjA4Mgd1bmkyMDgz"
        "B3VuaTIwODQHdW5pMjA4NQd1bmkyMDg2B3VuaTIwODcHdW5pMjA4OAd1bmkyMDg5B3VuaTIwNzAHdW5pMDBCOQd1bmkwMEIy"
        "B3VuaTAwQjMHdW5pMjA3NAd1bmkyMDc1B3VuaTIwNzYHdW5pMjA3Nwd1bmkyMDc4B3VuaTIwNzkHdW5pMDBBMAlleGNsYW1k"
        "YmwWcGVyaW9kY2VudGVyZWQubG9jbENBVBtwZXJpb2RjZW50ZXJlZC5sb2NsQ0FULmNhc2UHdW5pMjExNg1jb2xvbm1vbmV0"
        "YXJ5BEV1cm8EbGlyYQZwZXNldGEHdW5pMjIxNQd1bmkyMTI2B3VuaTIyMDYHdW5pMDBCNQd1bmkwMzA4B3VuaTAzMDcJZ3Jh"
        "dmVjb21iCWFjdXRlY29tYgd1bmkwMzBCC3VuaTAzMEMuYWx0B3VuaTAzMDIHdW5pMDMwQwd1bmkwMzA2B3VuaTAzMEEJdGls"
        "ZGVjb21iB3VuaTAzMDQHdW5pMDMxMgd1bmkwMzI2B3VuaTAzMjcHdW5pMDMyOA51bmkwMzA4Lm5hcnJvdwAAAAABAAH//wAP"
        "AAEAAwASAAAAZAAAAMYAAAD4AAIADQABABUAAQAYACgAAQAqAD4AAQBAAGEAAQBjAJUAAQCXAKgAAQCqAK0AAQCvALYAAQC4"
        "AMAAAQDCAOQAAQDmAQMAAQEEAQkAAgIiAjIAAwAQAAYAGgAkACQAQgBCAFIAAgABAQQBCQAAAAEABAADAdoAHgACAAYAEgAD"
        "ATwABgAAABGAAAADAnkABgAAABKAAAABAAQAAwElAAYAAAAXgAAAAQAEAAMCMgAGAAAAD4AAAAEAAgAAAAwAAAAcAAIAAgIi"
        "AiYAAAIoAi4ABQACAAMCIgImAAACKAIuAAUCMgIyAAwAAQAAAAwAAQAAABYAAQABAABAAEAAAC8AAAABAADU1dbo7O3x8/T1"
        "9vj6/v8BAgMEBQYHCAkKCwwNDg8QERMUFRYYGhseISgrMkVIUgAAAAABAAAAEgAgAAoAAwAyACoAPAACREZMVAA+bGF0bgBU"
        "AANrZXJuADRtYXJrADpta21rAEAABAAAAAEAZgACAAgAAgCCAUYABgAQAAEAYAABAGIAAAAAAAEAAAAAAAEAAQAAAAEAAgBM"
        "AAhBWkUgAExDQVQgAExDUlQgAExLQVogAExNT0wgAExST00gAExUQVQgAExUUksgAEwAAQvKDCAABAxmIzwAAQvIC7gAAQve"
        "C7QAAP//AAMAAAABAAIAARLWAEQAAABdDvoO+g0YDOQM5A5SDlIOUiCmDMIMwgzQDNAMtA2KDYoNXg1eDKYMwgzCDMIMtB1I"
        "DLQNOA04DTgQjBCMDJgMmBcOEj4YEh6+E5QRPhE+FFwPOA84D7QQ4g7CDbwNvA28De4MiA3uDe4N7gz+DooOig4gDiAOIA4g"
        "EbgOwg7CDsIOwhA8EDwP+A/4D/gPdg92D3YVPAz+G94agBk0DIgWIgy0DKYNGAzCDTgMpg0YDLQMkAyQDJAQ4gzCAAIqLgBE"
        "AAAq4iyEAB4AFwAAAAAAAAAAAAAAAAAAAAAAAAAA/+wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/i"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/7AAA/+wAAAAAAAD/4gAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAA/+wAAAAAAAAAAAAA/4gAAP/sAAAAAAAA/+wAAP/YAAAAAAAAAAAAAP/sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAA/+IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/sAAD/7AAAAAAAAAAA"
        "AAD/2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+wAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAP/sAAAAAAAAAAAAAAAA"
        "AAAAAAAA/+IAAAAAAAAAAAAAAAAAAP/iAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/iAAA/+wAAAAAAAAAAAAA/9gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAA/+wAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/sAAD/4gAAAAAAAAAA"
        "AAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/9gAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAD/zgAAAAAAAAAAAAD/9gAA/84AAP/OAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA//YAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/4gAA/+IAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAD/zioWAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/iKhwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+IAAAAA"
        "AAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+wAAAAAAAAAAAAA/5wAAP/sAAAAAAAA/+wAAP/YAAAAAAAAAAAAAP/s"
        "AAAAAAAAAAAAAAAAAAD/7AAAAAAAAP/sAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/iAAAAAAAAAAA"
        "AAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+wAAAAAAAAAAAAA/+IAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAA"
        "AAAAAAAAAAAAAP+IAAD/7AAAAAAAAP/sAAD/ugAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/8QAAAAAAAAAAAAAAAAAAP/sAAAAAAAAAAAAAP/sAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/4gAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/84AAAAAAAAAAAAAAAAAAAAAAAD/4gAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+IAAAAAAAD/zgAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAD/7AAA/9gAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP/iAAAAAAAAAAAAAP/sAAAAAAAAAAAAAP/i"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/iAAA/+wAAP+wKhb/7AAA/5wAAAAAAAAAAAAAAAAAAAAA"
        "AAD/zgAA/+IAAP/iAAD/4gAA/5wAAP/OAAD/nCoo/9gAAP+mMHD/2AAAAAAAAP/sAAD/4gAA/9gAAP/EAAD/nAAA/+IqHP+c"
        "AAAAAAAAAAAAAP/OAAD/zgAAAAAAAAAAAAD/zgAA/7oAAAAAAAD/zgAAAAAAAAAAAAAAAAAAAAAAAP/iAAD/xAAA/+IAAP/i"
        "AAD/2AAA/34AAP/OAAAAAAAA/+IAAP+SAAAAAAAAAAAAAP/YAAAAAAAAAAAAAP/sAAD/4gAAAAAAAP/iAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAD/iAAA/+wAAAAAAAD/7AAA/84AAAAAAAAAAAAA/+wAAAAAAAAAAAAAAFAqIgAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAPCoiAAAAAAAAAAAAAAAA/84AAAAAAAD/nAAAAAAAAAAAAAAAAAAAAAAAAP+6AAAAAAAA/9gAAAAAAAAAAAAAAAAAAP+c"
        "AAAAAAAA/5wAAP+mAAAAAAAAAAAAAP/OAAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAD/iAAA/+wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/+wAAP/sAAAAAAAA/8QAAAAA"
        "AAAAAAAAAAAAAAAAAAD/7AAAAAAAAAAAAAAAAAAAAAAAAAAAAAD/zgAAAAAAAP/sAAD/2AAAAAAAAAAAAAD/7AAAAAAAAAAA"
        "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"
        "AAAA+/+6AAABAP/iAAAAFgBx/+IAAAB4/5wAAACB//YAAACI//YAAACL//YAAACN/+wAAACO//YAAACR//YAAACU/+wAAACV"
        "/+wAAACX/+wAAACY//YAAACh//YAAACj/+wAAACo/+wAAAC3/+wAAAC6/+wAAAC8/+wAAADK//YAAADV//YAAADY//YAAAD0"
        "/9gAAAAZAAH/7AAAAAj/7AAAAAv/7AAAAED/7AAAAGH/2AAAAGX/2AAAAHj/7AAAAIH/4gAAAIj/4gAAAIv/4gAAAI7/7AAA"
        "AJH/4gAAAJj/4gAAAKH/4gAAAKP/7AAAAMr/4gAAANX/4gAAANj/4gAAAOT/4gAAAOb/4gAAAOj/4gAAAOn/7AAAAPH/7AAA"
        "AQD/7AAAAaT/2AAAAAEAXQABAAgADQAOABEAFAAWABcAIgAoACkAKwA0ADYAOAA5ADoAPQBAAEEARABGAEcAUwBVAFoAXQBf"
        "AGEAZQBmAG4AcQByAHcAeAB9AIEAiACNAI4AkQCUAKIAowCoAKkAqgCrAKwArQCuAK8AtwC6ALsAvAC9AL8AwADCAMMAxwDJ"
        "ANgA2QDcAN0A4ADiAOQA5gDoAOkA8QD0APUA+gD7AQABowGkAaYBpwGpAaoBqwGsAd4B3wHgAfQB/QAhAGH/7AAAAGX/7AAA"
        "AHH/4gAAAIH/2AAAAIj/2AAAAIv/2AAAAI7/2AAAAJH/2AAAAJj/2AAAAKH/2AAAAKz/7AAAAML/7AAAAMP/7AAAAMf/7AAA"
        "AMn/7AAAAMr/2AAAANX/2AAAANj/2AAAANn/7AAAAN3/7AAAAOD/7AAAAOL/7AAAAOT/2AAAAOb/2AAAAOj/2AAAAOn/7AAA"
        "APH/7AAAAPT/4gAAAPX/7AAAAPr/7AAAAPv/7AAAAQD/7AAAAaT/7AAAACUAWv/sAAAAXf/sAAAAX//sAAAAYf+IAAAAZf+I"
        "AAAAcf/iAAAAeP+cAAAAgf/sAAAAiP/sAAAAi//sAAAAjv/sAAAAkf/sAAAAmP/sAAAAof/sAAAAo//sAAAArP/2AAAAvP/s"
        "AAAAwv/2AAAAw//2AAAAx//2AAAAyf/2AAAAyv/sAAAA1f/sAAAA2P/sAAAA2f/sAAAA3f/sAAAA4P/sAAAA4v/sAAAA5P/O"
        "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"
        "/5wAAADj/9gAAADp/8QAAADx/8QAAAD0/7oAAAD1/7AAAAD6/6YAAAD7/6YAAAEA/6YAAAGj/+wAAAGp/+wAAAGs//YAAAHM"
        "/6YQjgHN/6YQjgHQ/6YQjgHh/6YQjgHo/6YQjgHp/6YQjgH0/9gAAAH9/+wAAAH//+wAAAIE/+wAAAIF/+wAAAIH/9gAAAD4"
        "DSIAABBmDTYNIgAADSwNNg0iAAANLA02DSIAAA0sDTYNIgAADSwNNg6uAAANLA02DSIAAA0sDTYP2gAAEGYNNg0iAAANLA02"
        "DSIAAA0sDTYNQAAAD7wAAA1AAAAPvAAAD9oAABBmAAAOXgAAEGYAAA5eAAANSgAADl4AAA1KAAAOXgAAEGYAAA5eAAANSgAA"
        "Dl4AAA1KAAANVAAAEGYAAA1UAAANXgAADXIAABBmDRANcgAADRANEA1yAAANEA0QDXIAAA0QDRANcgAADRANEA1yAAANEA0Q"
        "DXIAAA0QDRANaAAADRANEA1yAAANEA0QDXwAABBmDRAOpAAAEGYAAA/aAAAQZgAAD9oAAA2QAAAP2gAADZAAAA2GAAAQZgAA"
        "D9oAAA2QAAAP2gAAEGYAAA/aAAANmgAADcIAAA24DcwAAAAAAAANzA3CAAANzA3MDcIAAA24DcwNwgAADcwNzA3CAAANzA3M"
        "DcIAAA24DcwNpAAADcwNzA3CAAANzA3MDa4AAA24DcwNwgAADcwNzA/aAAAQZgAAD9oAAA0WAAAP2gAAEGYAAA3WAAAQZgAA"
        "DeAN6hBmAAAN4A3qEGYAAA3gDeoQZgAADeAN6hBmAAAN4A3qEGYAAA6QAAAQSAAAD9oAABBmAAAP2gAADf4AAA/aAAAN/gAA"
        "DfQAABBmAAAP2gAADf4AAA/aAAAQZgAAD9oOfBBmD6gP2g58DRYPqA/aDnwNFg+oD9oOfA0WD6gP2g58DRYPqA6uDnwNFg+o"
        "D9oOfA0WD6gP2g58DRYPqA4IAAAOEgAADggAAA4SAAAP2g58DRYPqA4iAAAPvAAADqQAABBmAAAOpAAAEGYAAA6kAAAQZgAA"
        "D9oAABBmAAAP2gAADiwAAA/aAAAOLAAADjYAABBmAAAP2gAAEGYAAA/aAAAQZgAAD9oAABBmAAAP2gAAEGYAAA/aAAAQZgAA"
        "D9oAABBmAAAOQAAADkoAAA/aAAAQZgAAD9oAAA5UAAAP2gAAEGYAAA5eAAAQZgAAD9oOfBBmDoYP2g58DoYOhg/aDnwOhg6G"
        "D9oOfA6GDoYP2g58DoYOhg5oDnwOhg6GD9oOfA6GDoYP2g58DoYOhg5yDnwQZg6GD9oOfA6GDoYP2g58DoYOhg6kAAAQZgAA"
        "DpAAABBIAAAOkAAAEEgAAA6QAAAQSAAADpAAABBIAAAOmgAAEEgAAA6kAAAQZgAAD9oAABBmAAAP2gAAEGYAAA/aAAAQZgAA"
        "D9oAABBmAAAOrgAAEGYAAA/aAAAQZgAAD9oAABBmAAAP2gAAEGYAAA/aAAAQZgAADswAAA7CDuAOzAAADtYO4A7MAAAO1g7g"
        "DswAAA7WDuAOzAAADtYO4A64AAAO1g7gDswAAA7CDuAOzAAADsIO4A7MAAAO1g7gDswAAA7WDuAO6gAADvQAAA7qAAAO9AAA"
        "EFwAABBmAAAQXAAAEGYAABBcAAAQZgAAEFwAABBmAAAQXAAAEGYAABBcAAAQZgAAEFwAABBmAAAP2g7+EGYAAA/aDv4QZgAA"
        "DwgAAA0cAAAQXAAAEGYQZhBcAAAQZg+oEFwAABBmD6gQXAAAEGYPqBBcAAAQZg+oEFwAABBmD6gQXAAAEGYPqBBSAAAQZg+o"
        "EFwAABBmD6gQXAAAEGYQZg/aAAAQZgAAEFwAAA8SAAAQXAAADxIAABBcAAAPEgAAEFwAAA8SAAAQXAAADxIAAA/aAAAQZgAA"
        "D9oAABBmAAAAAAAAD1gAAA8mAAAPWA8wDyYAAA9YDzAPJgAAD1gPMA8mAAAPWA8wDyYAAA9YDzAPHAAAD1gPMA8mAAAPWA8w"
        "DyYAAA9YDzAPJgAAD1gPMAAAAAAPWAAADzoAAA9EAAAPOgAAD0QAAA/aAAAQZgAAD9oAABBmAAAPTg9sD1gAAA9OD2wPdgAA"
        "D04PbA9YAAAPYg9sD1gAAA9iD2wPdgAAEDQAABBIAAAQXAAAEGYAABBcAAAQZgAAD4AAAA+KAAAQXAAAEGYAABBcAAAQZgAA"
        "EFwAABBmAAAPlAAAD54AABBcEAwQZg+oEFwQDBBmD6gQXBAMEGYPqBBcEAwQZg+oEFwQDBBmD6gQUhAMEGYPqBBcEAwQZg+o"
        "EFwQDBBmD6gQXAAAAAAAABBcAAAAAAAAEFwQDBBmD6gPsgAAD7wAABBcAAAQZgAAD9oAABBmAAAQXAAAEGYAABBcAAAQZgAA"
        "EFwAABBmAAAQXAAAEGYAABBcAAAQZgAAD9AAABBmAAAP0AAAEGYAAA/QAAAQZgAAD8YAABBmAAAP0AAAEGYAAA/QAAAQZgAA"
        "D9oAABBmAAAP5A/4EAIAAA/kD/gQAgAAD+QP+BACAAAP7g/4EAIAABBcEAwQZhAWEFwQDBAWEBYQXBAMEBYQFhBcEAwQFhAW"
        "EFwQDBAWEBYQUhAMEBYQFhBcEAwQFhAWEFwQDBAWEBYQXBAMEGYQFhBcEAwQFhAWEFwQDBAWEBYQIAAAECoAABA0AAAQSAAA"
        "EDQAABBIAAAQNAAAEEgAABA0AAAQSAAAED4AABBIAAAQXAAAEGYAABBcAAAQZgAAEFwAABBmAAAQXAAAEGYAABBcAAAQZgAA"
        "EFIAABBmAAAQXAAAEGYAABBcAAAQZgAAEFwAABBmAAAQXAAAEGYAAAAAACuAAAAAACeAAAAAACmAAAAAACGAAAABAFgAAQAI"
        "AA0ADgARABQAFgAXACgAKQArADQANgA4ADkAOgA9AEAAQQBEAEYARwBVAFoAXQBfAGEAZQBmAG4AcgCBAIgAjgCRAJQAlQCi"
        "AKMAqACpAKoAqwCsAK0ArgCvALcAugC7ALwAvQC+AL8AwADCAMMAxwDJANgA2QDcAN0A4ADiAOQA5gDoAOkA8QD1APsBowGk"
        "AaYBpwGpAaoBqwGsAcwB0AHeAd8B4AHhAfQB/QACAEUAAQABABAACAAIABAADQANAAcADgAOABEAEQARABEAFAAUAAgAFgAX"
        "AAgAKwArABIANAA0ABIANgA2AAIAOAA5ABMAOgA6ABQAPQA9ABQAQABAAAkARwBHAAIAVQBVAAIAWgBaAAMAXQBdAAMAXwBf"
        "AAMAYQBhABUAZQBlABUAZgBmAAoAbgBuAAoAcgByAAoAgQCBABYAiACIABYAjgCOABcAkQCRABcAlACUAB0AlQCVABgAogCi"
        "ABkAowCjAAEAqACqAA0AqwCrAAQArACsABoArQCvAAQAtwC3AAUAugC7ABsAvAC9AAYAvgC+ABgAvwDAAAYAwgDDAAEAxwDH"
        "AAEAyQDJAAEA2ADYAAEA2QDZABwA3ADcABwA3QDdAA4A4ADgAA4A4gDiAA4A5ADkAA8A5gDmAA8A6ADoAA8A6QDpAAUA8QDx"
        "AAUA9QD1AAUA+wD7ABoBowGjAAIBpAGkAAkBpgGmAAcBqQGpAAMBqgGqAAkBqwGrAAcBrAGsAAIBzAHMAAsB0AHQAAsB3gHg"
        "AAwB4QHhAAsB9AH0ABkAAgBgAAEAAQAOAAgACAAOAAsACwAOAA0ADgADABEAEQADABQAFAAEABYAFwAEABgAGAADACEAIQAD"
        "ACIAIgAEACMAIwADACYAJgADACgAKQAEACsAKwAUADQANAAUADgAOQAIADoAOgALAD0APQALAEAAQAAIAEEAQQAEAEQARAAE"
        "AEYARgAEAEcARwADAFIAUgADAFMAUwAIAFQAVAAEAFUAVQADAFYAVgAIAFkAWQAIAFoAWgAMAF0AXQAMAF8AXwAMAGEAYQAP"
        "AGUAZQAPAGYAZgALAG4AbgALAHIAcgALAIEAgQAFAIIAhwAHAIgAiAAFAIkAigAHAIsAiwAFAI0AjQACAI4AjgAFAI8AkAAB"
        "AJEAkQAFAJIAkwABAJQAlQAQAJcAlwAQAJgAmAAFAJkAoAABAKEAoQAFAKIAogACAKgAqgACAKsAqwANAKwArAAKAK4ArwAN"
        "ALIAsgANALoAwAACAMIAwwAKAMcAxwAKAMkAyQAKAMoAygAFAMsA0QABANQA1AABANUA1QAFANcA1wACANgA2AAFANoA2wAV"
        "AN0A3QASAOAA4AASAOIA4gASAOMA4wACAOQA5AATAOYA5gATAOgA6AATAOkA6QAWAOoA8AAGAPEA8QAWAPIA8wAGAPUA9QAK"
        "AaMBowADAaQBpAAPAakBqQADAawBrAAMAcwBzQAJAdAB0AAJAd4B4AARAeEB4QAJAegB6QAJAfQB9AACAf0B/QAEAf8B/wAE"
        "AgQCBAADAgUCBQAEAgcCBwAIAAMAhQKtA/wEAgADALIBwgQiBCgAAwClAcID9AQeAAMAmgHCBAgEFAADAJYBwgPmBAoAAwCZ"
        "AcID4gQAAAMAmAHCA9ID9gADAJ4BwgPUA+wAAwCTAcID0APiAAMAlgHCAAAD2AADAJIBwgAAA84AAwCoAcIDygPEAAMAkgAA"
        "A9IAAAADAJUAAAO8AAAAAwCUAAADuAAAAAAABIAAAAEBJP/0AAEBJAAAAAEBLwAAAAMBJQJ2A5wEOAADAdv/9AQ0AAAAAwHb"
        "//QEJAAAAAMB9AJqANwDhAADASQAAAQQA+wAAwD6AncDLgPoAAMA+f/0AAAD5AADAPgDVgPUA/gAAwElAncDIgPKAAMBJQJr"
        "AxgDTgADASsCdwOqA7YAAwEs/+ADUgMEAAMBJAAAA8YDnAADAJsDYgNiAuoAAwDIAncCwgOOAAMAygAAAugAAAADAMoCdwLe"
        "A3oAAwDF//QDXgAAAAMBJQJ3AvoDZgADASUCdwLeAvYAAwGQAZoC8gNeAAMBJQJ3A2wDSAADASX/9ANiAAAAAwE4ArwAFAAA"
        "AAMBOAAAAAoAAAAAAAqAAAADAdsCdwM4AxoAAwEk/+IDFgKSAAMBKgJ3AqwDBgADAUgCdwJ+AvwAAwFIAAACdAAAAAMBJP/g"
        "AqwCNAADASUCdwLeAt4AAwD1A2ICmAImAAMBIgJ3AugCygADAjYCvAIqAAAAAwEi//QC1AK8AAMBgAJ3AtACrAADAVMDYgLA"
        "AfQAAwElArwCGgAAAAMA+ANiAfIB4AADAQECrQIqAb4AAwFTAAACegAAAAMBLgHCAAAB2gADAjUAAAJsAAAAAwI1AAACXAAA"
        "AAMCcQHCAf4BvAADAjYAAAJOAAAAAwJpAZsB8AH2AAMBLwJ3AAACNAADASX/PwGsAY4AAwBCAq0CDgFaAAMAbwHCAfIBgAAD"
        "AG3/9AHKAioAAwF9AcIBWgFsAAMA+P8/AbwBXAADAG8CdwHKAe4AAwBvAAABwAAAAAMAbgJ3AaQB2gADAP0BmgGgAaYAAwBv"
        "/+QBogFCAAMBfAHCAZ4BJgADAXwAAAGUAAAAAwElAcIBkAESAAMBJQAAAYYAAAADAg8ACgGCAAAAAwHbAcIBfgD0AAMB2wAA"
        "AXQAAAADAUwBwgFwAOAAAwFNAcIAygDWAAMBJQJ3AOQBYgADAVMBwgFYAMIAAwF4AncA0AFOAAMCYAGaAUoBUAADAXIAAAFM"
        "AAAAAwI2AcIAmgCaAAMBKP/0AUoBPgADASABwgCeAIYAAwEgAAAAlAAAAAMBgAHCASwAcgADAVMCrQEcADgAAwGAAAABGAAA"
        "AAMA+AKtAE4AJAADASUBwgBiAEoAAwElAAAAWAAAAAAAG4AAAAAAEoAAAAAAB4AAAAAADoAAAAAADYAAAAAADIAAAAAAE4AA"
        "AAAAJoAAAAAAGYAAAAAAH4AAAAAABYAAAAAAI4AAAAAABoAAAAAAF4AAAAAAC4AAAAAAIIAAAAAAJYAAAAAALIAAAAAAKoAA"
        "AAAAEIAAAAAACYAAAAAAJIAAAAAAAIAAAAAAIoAAAAAAGIAAAAAAHYAAAAAAKIAAAAAAAoAAAAAAHIAAAAAALYAAAAAALoAA"
        "AAAAHoAAAAAAEYAAAAAACIAAAAAAFYAAAAAAD4AAAAAAAYAAAAAAA4AAAAAAGoAAAAAAFoAAAAAAFIAAAAEAAAAKAEQAGAAC"
        "REZMVACQbGF0bgE+ABUAhgCOAP4BCAESARwAlgCeAKYArgC2AL4AvgDGAM4BJgDWAN4A5gDuAPYADmFhbHQBdGNhc2UBOGNj"
        "bXABfGNjbXABjGRsaWcBPmZyYWMBRGxpZ2EBSmxvY2wBUGxvY2wBVmxvY2wBXG9yZG4BhHNpbmYBYnN1YnMBaHN1cHMBbgFA"
        "AAAAAQAAAAECGAADAAAAAQIqAAEAAAABAUYAAQAAAAEBkAAGAAAAAQFaAAEAAAABATQAAQAAAAEBMgABAAAAAQEwAAEAAAAB"
        "AS4ABAAAAAEBiAABAAAAAQFmAAQAAAABASoAAQAAAAEBFAAEAAgAAQEiAAQACAABASIABgAQAAEBjAAAAAEAEAABARgAAAAC"
        "ABAAAQEUAAAAAgAQAAEBLgAAAAYAAAACAUABUgGkAAhBWkUgAfZDQVQgAb5DUlQgAfZLQVogAfZNT0wgAdpST00gAdpUQVQg"
        "AfZUUksgAfYAAAABABIAAAABABMAAAABAA4AAAABABQAAAABAAYAAAABAAgAAAABAAcAAAABAAwAAAABAAsAAAABAA0AAAAC"
        "AAAAAQAAAAIAAgAFAAAAAgAPABEAAAADAAIABQAFAAD//wAKAAAAAQACAAQABQAGAAoACwAMAA0AAQFmAAYAAQFsAAcAAQFm"
        "AAgAAQGIABMAAQGCAB0AAQFOAAEAAQFOAAEBXAABAqAAAQFMAAECigABAUgAAQK4AAEBaAABAUIAAQABAUQAAQFKAAIBjgAE"
        "AF8AZQDiAOgAAgGMAAQBCgELAQoBCwABAWYAAwFCAUgBTgABAugABQF+AUgBVAFMAVAAAwABARAAAQGEAAAAAQAAABAAAwAB"
        "AP4AAQF6AAAAAQAAABAAAgFwAVQBzAG8AAQBMAFcAVgBXAACAd4ACgEKAQsAXwBlAQoAsQELAOIA6AHdAAEBtAALAUwBUgFY"
        "AV4BZAFqAXABdgF8AYIBiAAA//8ACgAAAAEAAwAEAAUABgAKAAsADAANAAD//wALAAAAAQACAAQABQAGAAgACgALAAwADQAA"
        "//8ACwAAAAEAAgAEAAUABgAJAAoACwAMAA0AAP//AAsAAAABAAIABAAFAAYABwAKAAsADAANAAEAAQCrAAEAAQHcAAEAAQHV"
        "AAEBTAABATwAAgGSAaIAAQACAKsAtwABAAEAtAACAKwCMQACAAEBowGsAAAABQFAAUgBLgE0AToAAgA6Ad0AAgCiAKsAAgCi"
        "ALwAAQE4AAEBPAABAUAAAgFkAWwAAQADAD4BBwEIAAAAAQAEAF0AZADgAOcAAQAEAAEARwCBAMoABAFKAVIBWgFiAAIAAAAB"
        "AWoAAQF0AAEAAgABAIEAAQACAEcAygABAAMAqwC0ALcAAgG2AcAAAgG3AcEAAgG4AcIAAgG5AcMAAgG6AcQAAgG7AcUAAgG8"
        "AcYAAgG9AccAAgG+AcgAAgG/AckAAgHcAd0AAgACAiICJgABAigCLgABAAIAAwCrAKsAAwC0ALQAAgC3ALcAAQACAAIBowGs"
        "AAAB1QHVAAoAAQAKAAEARwBdAGQAgQCrAMoA4ADnAdwBCQACAOQAAQABAN0B/wADAMoBzAABAAEAQQEEAAIAogEHAAIAqwEI"
        "AAIAvAEFAAMAogCrAQYAAwCiALwAAQABAKIBrwADAdoBpgG0AAMB2gGrAbUAAwHaAasAAQC8AAEAAQC8AAEAAAAJAAEAOgAB"
        "AAEAOgABAAAACgGzAAMB2gGrAbEAAwHaAacBsgADAdoBqwGwAAMB2gGnAa4AAwHaAaYBrQADAdoBpQABAAUBpAGlAaYBqAGq"
        "AAAAAQABAAEAAQAAAAQAAAABAAEAAQABAAAAAwABAAAAAAAUAAAAaQAAAAAAAAAAAAEAAAAQAAIAAAAaAAAASQABAAEAAEAA"
        "QAAAJwAAAAEAAOwBAgYHCAkKCwwNDg8QERMUFRYXGRobHB0iJCcoLzEyOEJDUFdofAACAAEAAQAAAAAAowAFAjkVDg4ODg4O"
        "Dg4ODgQEDg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4ODg4OEhsSEhISEhISEhIODg4ODg4ODg4VCQ4ODg4ODg4ODg4ODg4O"
        "AAAOBA4ODg4ODg4ODg4ODg4PDhwODg4ODg4ODg4ODg4ODg4JCQkJCQ4ODg4ODg4ODg4MDAwMDAwMDAwMAgIODg4ODg4ODg4N"
        "QA4ODg4ODg4ODg4ODg4ODg4ODg4WFhYWFhYWFhYWFh0QEBAODhYWFhYWHgkODiMODg5BDg4ODg4ODg4ODg4EDg4ODg4ODg4O"
        "Dg4ODg4MDAwMDA4ODg4ODg4ODg4ODgkJCQkJDg4ODg4ODg4ODgQEBA4OAggIDg4OEBAQDA4OBA4ODg4ODAkODg4ODg4ODg4O"
        "CQgODgkLDAQODg4RDgsHDgsaDg4QDg4MDg4JDg4ODg4MCQ4ODg4ODg4ODw4ODQkIDg0JDQQHDg4OFhEQDQcOGw4OEA4ODg4S"
        "Dg4JDhIODg4JDg4ODg4ODg4SDg4LEQ4ODg4ODg4ODhYODg4NDgQODQ4ODg4OCQ4OCQ4ODg4SDg4ODg4ODg4KIhUGFCIhIAMT"
        "FR8fEx8fExMfExUfHxMfHxMfH0BAFhUWFQ0WFg4OFhUVEgwPDxcYGRIQERISEBASEhUREREVFQUFDg4RFQ4kDAkOHh4JFBYS"
        "AQ4OJg4ODQxADBwPQEBAQEBAQEBAQEBAQEAVBAsODgkOBgxAQEBAQEBAQEBAQEBAQEBAQEBAFEAlQAAAAAEAAAAAAAgAAQAU"
        "eGhndAAAAAAAAQAAAAwAAQAAABYAAQABAABAAEAAAAEAAAABAAARAAABAAEACAABAAAAFAAEAAAAHAACd2dodAEAAAAACAAY"
        "ACQAMAADAAAAAgEBAZAAAAK8AAAAAQAAAAABAgH0AAAAAQAAAAABAwJYAAAAAQAAAAABBAK8AAAAAQAAABAAAgABABQABAAI"
        "d2dodAGQAAABkAAAArwAAAAAAQABAQAAAZAAAAECAAAB9AAAAQMAAAJYAAABBAAAArwAAAABAAAAAQABAAAElgJAAAAAAASY"
        "AAAADgAmAC8AOABBAEoAUwBcAGQAbQB2AJoAowC9ANQA3QDmAO4A9wEAARUBHgElAUABWgFjAWwBdQF+AYcBkAGZAaIBqgG8"
        "AdcB4AHpAfAB+QINAiQCLQI+AkYCTwJYAmECagJzAnwChQKNApYCqgKzAs4C1QLmAu8C+AL/AwgDJANBA1UDXgNnA24DdwOR"
        "A6UDrgO3A8ADyQPSA9sD5AP8BAUEDgQxBEMEVQRsBIQEjQSWBJ0EugTDBMwE1ATdBOQE/gUSBSsFNAU8BUMFVAVdBWYFbwV4"
        "BYEFigWTBZwFpQWuBcIF3wXoBfEF+gYDBiMGNwZABkkGUgZbBnUGfgaHBpAGpQatBrUGvQbEBswG1AbcBuQG7AcVBx0HLwdG"
        "B04HVgdeB2YHbgeCB4sHpQfCB9wH5AfsB/QH/AgECAwIFAgcCCQIOAhSCFoIYghqCHIIhgigCKkItwjCCMoI2wjjCOsI8wj7"
        "CQMJDQkVCSkJOglLCVMJawlyCX0JhgmPCZcJngmyCccJ1gneCeYJ7gn1Cf0KEwonCi8KNwo/CkcKTwpXCl8KeQqBCokKrAq+"
        "CtAK4grzCvsLAwsKCyELKQsxCzkLQQtIC2ALdQuSC5sLowurC7wLxAvMC9QL3AvkC+wL9Av9DAUMDQwhDDYMPgxGDE4MVgx2"
        "DI0MlQydDKUMrQzBDMkM0QzZDPYNFA0xDUYNWg17DZMNqg3CDdoN9A4ADgwOGg4sDkYOZg6MDqkOvQ7XDvoPFQ8kD0EPUg9k"
        "D3sPjw+jD70P2A/4EAkQHhA7EFwQbRB/EJQQqRC4EM8Q5hEDESARMRFFEVwRdhGOEaMRuxHVEe8R+xITEicSORJTEnMSnxK5"
        "Es0S5xL/Ex8TLhNLE1wTcBN+E5ATpxO7E9IT7xQKFCoUORROFGIUfRSOFKIUtxTLFOAU+BUPFSkVQxVRFWIVcxWIFaIVuhXY"
        "FfAWChYWFjAWShZeFnUWhhahFrUW0hbmFvcXCxcZFysXSBdcF3AXkRexF8YX5BgEGBsYLxhGGF0YdRiMGKYYuhjSGOYY/RkX"
        "GTIZQRlYGWYZfhmSGaYZxBnYGfUaBxoZGi0aRBpfGn8alBqpGr0a1BrsGwMbIBsyG0YbVxt0G5EboxvAG9ob6xwIHCIcURyA"
        "HLoc4x0VHUQdfx28HfEeBR4WHjIeTh5lHoEemh6rHs4e5x77HwwfKB9EH1sfeh+TH6Qfvx/YH9gf2B/iH/QgAiAXICcgNSBD"
        "IFogcSB8IIcgoyC3INcg4CDpIPMg/SEIIRMhHiEoITwhUSFsIYchmCGpIbsh2CH1IhIiJCI2IloigSKWIq0iyiLcIvEjICNK"
        "I1wjdyOZI7Mj3CPwI/skDCQvJEgkfCScJLwk2STuJQklPiVWJXcliCWXJaAlsyXCJc4l4CX1JgkmJCY/JlEmWCZnJnImgyag"
        "JrImxybVJvInASckJ1MnXydoJ3YnhCeZJ6onuSfIJ9Qn4CfvJ/goCSgaKDIoSihWKGIoayhyKIAolyidKKMoryi7KMEoyiji"
        "KPpAAIABAAgADAAABQQBAgICAoADGgAaGgQJARvvAACAAQAIABoAAAwLAAIDAgMCAgMCAgICCwH+/BYR9xv3+RkbEQsBEQcM"
        "EQH3G+jj6ACAAQAIAAYAAAIBAQIBFhEB9wCAAQAIAAYAAAIBAQIBCxEB9wCAAQAIAAYAAAIBAQIBChEB9wCAAQAIAAYAAAIB"
        "AQIBCREB9wCAAQAIAAYAAAIBAQIB/hEB9wCAAQAIAAYAAAIBAQIBBBEB9wCAAQAIAAcAAACAAhcAEYGFgAEACAAGAAACAQEC"
        "AQoRAfcAgAEACAAGAAACAQECAQ8RAfcAgAEACAAqAAAUEwEEAgICAgICAgICAgICAgICAgICEwH8FvQKB+0RDBHtBwr3G/f5"
        "GRsHExEHDAcM6OMW8iUg/AH3ARvo4+gAgAEACAAGAAACAQECAQMHAecAgAEACAAcAAANDAECAgICAgICAgICAgIM/gH+FBEU"
        "ERT3/PcbEQz8DAcM7Rv8ASXyFuMAAIABAAgAGAAACwoBAgICAgICAgICAgr+Af4UEfcb9xEUEQr8DAcM7eMlG/wBAACAAQAI"
        "AAYAAAIBAQIBFBEB9gCAAQAIAAYAAAIBAQIBCBEB9gCAAQAIAAcAAACAAvMAEYGFgAEACAAGAAACAQECAQgRAfYAgAEACAAG"
        "AAACAQECAQgRAfYAgAEACAAWAAAKCQECAwIDAgICAgIJARYRFBb59/kbEQkHDBH3ASUg6OMAgAEACAAIAAAAggARgYAA9oMA"
        "gAEACAAFAAAAgQARgYSAAQAIAB4AAA4NAQICAgMCAwICAgICAgKADAEAFRATFfj2+BoiGhEN8xcHDBH3ASUg6OMX8wCAAQAI"
        "ABwAAA0MAQICAgICAgICAgICAgz+Af4UEfcbFhv3ERQRDPwMBwzo4xbyJSD8AQAAgAEACAAGAAACAQECARIRAfYAgAEACAAG"
        "AAACAQECAQcRAfYAgAEACAAGAAACAQECAQYRAfYAgAEACAAGAAACAQECAQYRAfYAgAEACAAGAAACAQECAQURAfYAgAEACAAG"
        "AAACAQECAQYRAfYAgAEACAAGAAACAQECAfoRAfYAgAEACAAIAAAAggARgYAA9oMAgAEACAAHAAAAgAIPABGBhYABAAgAEgAA"
        "CAcBAgICAgICAgcBFBH3GxYbEQcHDOjjFvIBAIABAAgAHgAADg0AAgICAgICAgICAgICAw3+Af4UEfcb9/n8FhEUEQ0B/AwH"
        "DO3jJffyFhv8AIABAAgABgAAAgEBAgELEQH2AIABAAgABgAAAgEBAgEKEQH2AIABAAgABQAAAIIAEYGFgAEACAAGAAACAQEC"
        "AQoRAfYAgAEACAAUAAAJCAECAgICAgICAggBG/wW9xH3GxEIBxYRFgcB8gEAAIABAAgAGAAACwoBAgICAgICAgICAgoBG/cR"
        "9xv8FvcbEQoHCQcB8gEWERblAACAAQAIAAYAAAIBAQIBChEB9gCAAQAIABAAAAcGAQICAgICAgYB/gEWGRYWBiXjB+MlAQAA"
        "gAEACAAHAAAAgAIWACeBhYABAAgABgAAAgEBAgEYFgH2AIABAAgABgAAAgEBAgENFgH2AIABAAgABgAAAgEBAgEMFgH2AIAB"
        "AAgABgAAAgEBAgELFgH2AIABAAgABgAAAgEBAgEMFgH2AIABAAgACAAAAIIAFoGAAPaDAIABAAgABgAAAgEBAgEGFgH2AIAB"
        "AAgABwAAAIACHwAWgYWAAQAIAAYAAAIBAQIBERYB9gCAAQAIABQAAAkIAQICAgICAgICCP4BG/f+FBEUEQj8GyXjBwz8AQAA"
        "gAEACAAGAAACAQECAQoRAfYAgAEACAAeAAAODQECAgICBAIFAgQCBQICDQH+GRv89xEZFhH3/BsRDQwH4xYRB+PtGyUB8gEA"
        "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"
        "AgICAgII/BsB/hQR9xYRCO3yFhEW8u0BAACAAQAIABgAAAsKAAICAgICAgICAgMK/gEb9/4BG/cRFBEKCwYqL/z3ESARBgAA"
        "gAEACAAgAAAPDgACAgICAgICAgIDAgICAg7+ARv3/gEb9xEU/hn4FBEOCwYqL/z3ESARBgYJBuIAAIABAAgAHgAADg0BAwIC"
        "AgICAgMCAgICAg38Af78FxUSFRf8G/gXEw379hINBw0S9gEf6R/pAIABAAgAJAAAERABAwICAgMCAgIDAgICAwICAhAB/P4B"
        "Gxn59xEWFBH3+RkbERAlIPLtERsWEe3yICUB9/wBAACAAQAIAA4AAAYFAAQCAgIDBfcBG/cREQUB9xEgEQCAAQAIABYAAAoJ"
        "AAEBAgICAgICAwn19ccBG/cREBEQCQb5+AERJREqAwCAAQAIABQAAAkIAQICAgICAgICCAEb+RTyDPcWDAgRJRElEQH8AQAA"
        "gAEACAAeAAAODQABAQICAgICAgICAgIDDfDwwvcWARv5FPIMCwwLDQb5+AH8ARElESURKgMAgAEACAAQAAAHBgECAgICAgIG"
        "/AEb9xEWEQYBESURAQYAAIABAAgAFAAACQgBAgICAgICAgII/gAbExET9hsQCPwHERb8ASXtAACAAQAIABYAAAoJAAICBAID"
        "AgICAgn+ARsRFPcb8gwMCQH8Bxb8Je0HAQCAAQAIABQAAAkIAAMCAgMCAwICCPz/ABkPEvUZEAgB4wcRFvwl7QAAgAEACAAW"
        "AAAKCQACAgQCAgIDAgIJAf4RBwr3Ge0RBwkBJREb/AHtJfIAgAEACAAaAAAMCwACAgIDAgICAQMCAgsBG/kUDAoM+RvvFAoL"
        "AREbERv8Aff3JfIAgAEACAAYAAALCgACAgICAgICAgIDCgH3/gH+ERsUERQRCgEl9/IWEe0bIPwAAIABAAgAHAAADQwBAgIC"
        "AgICAgICAgICDP4B/hQR9xsWG/cRFBEM/BYRFvLtG/clIPwBAACAAQAIABwAAA0MAQICAgICAgICAgICAgz+ARv3/PcbAf4U"
        "ERQRDPwgJfcb7fIWERb8AQAAgAEACAAMAAAFBAECAgICBAEbARsbBBEBB+MAAIABAAgAEAAABwYBAgICAgICBv0XABv6FBUG"
        "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"
        "DAsBAgICAgICAgICAgILARQRFBEU/Pf89xsRCwcM7Rv8ASXyFuMLAIABAAgAFAAACQgBAwIDAgICBAII+wAaGPj2EBURCPXr"
        "ER4UEesBAACAAQAIABgAAAsKAQIGAgICAgICAgIK/gH5FBYUERT3GxEK/BYH4xEW/AEl7QAAgAEACAAcAAANDAECAgICAgIC"
        "AgICAgIM/gH+FBH3GxYb9xEUEQz8FhEW8u0b9yUg/AEAAIABAAgAHgAADg0AAgICAgICAgICAgICAw359/4B/vf+FBEUG/cR"
        "EQ0LLwH8GxbjBwzt8iUgAIABAAgADgAABgUBAgICAgIF9xsBFBERBe0BERYLAIABAAgAGAAACwoBAgICAgICAgICAgr+Af4U"
        "ERT3G/wWEQr8FhEW/AEl7Rz2AACAAQAIAAwAAAUEAQICAgIEARsBGxsEEQEH4wAAgAEACAAaAAAMCwECAgICAwICAgMCAgsB"
        "G/z3ERkUEff8GxELBxsWEe3yICUB9wEAgAEACAAUAAAJCAEEAgMCAwICAggB/BYR9/kZGxEIIAcbIAHy9wEAAIABAAgAFAAA"
        "CQgBAwIDAgICBAII+wAaGPj2EBURCPzyESUbEfIBAACAAQAIACIAABAPAAICAgICAgICAgICAgICAw/69fz//P8AEg/1GRIZ"
        "9Q8QDwsvAfwb7eMHDOjjFvIlIACAAQAIABQAAAkIAQICAgICAgICCP4B/hQRFPcbEQj8FhEW/AEl7QAAgAEACAAgAAAPDgEC"
        "AgICAgICAgICAgICAg78GwH+FPQKB+0M8hH3FgcO7fIWERYRFvLtAe3y7QEAAIABAAgAEgAACAcBAgICBAICAgcB/hQRG/cb"
        "EQcWERb8CyXtAIABAAgAEgAACAcBAgICBAICAgf+Af4PFPcbEAf8FhHtASXtAIABAAgAFAAACQgBAgICAgICAgII/BsB/hQR"
        "9xYRCO3yFhEW8u0BAACAAQAIABgAAAsKAQICAgICAgECAQIK/gEb9xEU+wIUDREK/BElEfwBAQXp5AAAgAEACAAeAAAODQED"
        "AgMCAgICAgICAgICDfwB/vwWFBEUFvwb9xYRDfz3EQcMEff8ASDoIOgAgAEACAAkAAAREAEDAgICAwICAgMCAgIDAgICEAH8"
        "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"
    ),
    "PixelOperatorMono.ttf": (
        "AAEAAAAMAIAAAwBARkZUTXdtGeMAAEIwAAAAHE9TLzJViAlqAAABSAAAAGBjbWFwhVcZ2QAAA5QAAAKCZ2FzcP//AAMAAEIo"
        "AAAACGdseWaJbVi+AAAH/AAAMIxoZWFkDJg+ZQAAAMwAAAA2aGhlYQh+AfkAAAEEAAAAJGhtdHg4pDfcAAABqAAAAepsb2Nh"
        "RWBR6gAABhgAAAHkbWF4cAFQAG0AAAEoAAAAIG5hbWXeXmtuAAA4iAAABplwb3N0MatD0gAAPyQAAAMBAAEAAAfiGZn3k3DW"
        "Xw889QALBkAAAAAAypyZYwAAAADX20REAAD+1AMgBRQAAAAIAAIAAAAAAAAAAQAABRT+1ABIAyAAAAAAAyAAAQAAAAAAAAAA"
        "AAAAAAAAAAQAAQAAAPEAQAAQACgACgACAAAAAQABAAAAQAAAAAMAAgAEAyABkAAFAAAEEARgAAAA4AQQBGAAAAMAAE8BnggA"
        "AgAFCQAAAAAAAIAAACcAAAAKAAAAAAAAAABIRjk2AMAAICEiBRT+1ABIBRQBLAAAAAEAAAAAArwDhAAAACAAAQMgAGQAAAAA"
        "AyAAAAMgAAABLADIAGQAZAAAAGQBLAEsAGQAZABkAMgAyAEsAMgAZABkAGQAZABkAGQAZABkAGQAZAEsAMgAyADIAMgAZAAA"
        "AGQAZABkAGQAZABkAGQAZABkAGQAZABkAAAAZABkAGQAZABkAGQAZABkAGQAAABkAGQAZAEsAMgAZABkAAAAyABkAGQAZABk"
        "AGQAyABkAGQAZABkAGQAZAAAAGQAZABkAGQAZABkAMgAZABkAAAAZABkAGQAyAEsAGQAZAAAASwAZABkAGQBLADIAAAAZABk"
        "AAAAyABkASwAZAAAASwAyABkAGQAZABkAGQAZABkAGQAAABkAGQAZABkAGQAZABkAGQAZAAAAGQAZABkAGQAZABkAGQAAABk"
        "AGQAZABkAGQAZABkAGQAZABkAGQAZABkAAAAZABkAGQAZABkAGQAZABkAGQAZABkAGQAZABkAGQAZABkAAAAZABkAGQAZABk"
        "AGQAZABkAGQAZABkAGQAAABkAGQAZABkAGQAZABkAGQAZABkAGQAAAAAAGQAZABkAGQAZABkAGQAAABkAGQAZABkAGQAZABk"
        "AMgAyABkAMgAZADIAGQBLADIAMgAyABkAGQAZABkASwAZAAAASwAZABkAAAAAAAAAAAAAAAAAAMAAAADAAAAHAABAAAAAAF8"
        "AAMAAQAAABwABAFgAAAAVABAAAUAFAB+AKMApgCpAKwArgCxALgAuwD/AQkBDwEdASUBMQE1AUgBUwFZAV0BYQFlAW8BeAF+"
        "AscC2ALaAtwgFCAaIB4gIiAmIDAgOiCsILEgtyEXISL//wAAACAAoAClAKgAqwCuALAAtAC7AL8BCAEMARoBJAExATQBRwFS"
        "AVgBXAFgAWQBbAF4AX0CxgLYAtoC3CATIBggHCAgICYgMCA5IKwgsSC3IRchIv///+P/wv/B/8D/v/++/73/u/+5/7b/rv+s"
        "/6L/nP+R/4//fv91/3H/b/9t/2v/Zf9d/1n+Ev4C/gH+AODK4MfgxuDF4MLgueCx4EDgPOA339jfzgABAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAABBgAAAQAAAAAAAAABAgAAAAIAAAAAAAAAAAAAAAAAAAABAAADBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyAhIiMk"
        "JSYnKCkqKywtLi8wMTIzNDU2Nzg5Ojs8PT4/QEFCQ0RFRkdISUpLTE1OT1BRUlNUVVZXWFlaW1xdXl9gYQB6e31/h4ySl5aY"
        "mpmbnZ+eoKGjoqSlp6moqqyrsK+xsuVtZGUA53GVbGnwb2gAfI4AbgAAZnAAAAAAAAAAAJyudWNrAAAAAGp06GJ2eYvHyN3e"
        "4uPf4K0AtdUA7OrrAADmcuHk6XiAd4F+g4SFgomKAIiQkY/C2NwA2gDbcwAA2QAAAAAAFgAWABYAFgAoADwAaACqAPoBLAE6"
        "AV4BggGuAcQB1gHkAfACCgI6AlYCiAK6AuADBgMyA1oDkAO8A8gD1AP2BAoELARYBI4ErgTUBPoFFAUsBUAFZgV+BZQFtAXo"
        "BfgGJAZGBmgGhga2BuAHFAcmB0AHZAeIB8YH6ggUCCYIQAhSCHQIgAiSCLoI1gj8CRgJQAlcCYIJnAm2CdgKDAogCkIKWgp8"
        "CpgKtArQCwQLIAs6C14Lggu8C+AMCAwuDDwMYgyADIgMkgzCDPANJA04DUoNfg2KDZoN0g3wDgwOHg44DlIOXA50DoAOig6W"
        "DqIOrg66DsYO0g72DywPOA9ED1APXA9oD3QPgA+MD7APvA/ID9QP4A/sD/gQMhBuEHoQhhCSEJ4QqhDIEQARDBEYESQRMBE8"
        "EUgRjBHCEc4R2hHmEfIR/hIKEhYSIhJGElISXhJqEnYSghKOEqgS5BLwEvwTCBMUEyATPhNKE1YTYhNuE3oThhOsE7gTxBPQ"
        "E9wT6BP0FAgUFBRCFE4UWhR8FLQUwBTMFNgU5BTwFPwVCBUWFSIVLhU6FUYVUhVeFWoVghWaFbQV0hXaFeIV8BYEFhYWIBYs"
        "FjgWQhZYFnYWhBaUFvQXGBc6F3AXlhfoGBwYRgACAGQAAAJYA4QAAwAHAAABIREhASERIQH0/tQBLP5wAfT+DAMg/UQDIPx8"
        "AAIBLAAAAZADhAADAAcAAAEzESMVMxUjASxkZGRkA4T9RGRkAAIAyAJYAfQDhAADAAcAABMzESMTMxEjyGRkyGRkA4T+1AEs"
        "/tQAAAACAGQAAAK8A4QAGwAfAAATMxUzNTMVMxUjETMVIxUjNSMVIzUjNTMRIzUzBSMRM8hkyGRkZGRkZMhkZGRkZAEsyMgD"
        "hMjIyGT+1GTIyMjIZAEsZGT+1AAABQBk/zgCWARMAAMABwALAA8AKwAAATMVIwEzFSMBMxEjATMRIxMzFTMVIxEzFSMRMxUj"
        "FSM1IzUzESM1MxEjNTMB9GRk/nBkZAGQZGT+cGRkyGRkZGRkZGRkZGRkZGRkAyBk/gxkASz+1AK8/tQCWMhk/tRk/tRkyMhk"
        "ASxkASxkAAAADQAAAAACvAOEAAMABwALAA8AEwAXABsAHwAjACcAKwAvADMAAAEzFSsBMxUjAzMVIxMzFSMhMxUjNzMVIzcz"
        "FSM3MxUjNzMVIyUzFSsBMxUjAzMVIxMzFSMCWGRkZGRkZGRkZGRk/nBkZGRkZGRkZGRkZGRkZP7UZGRkZGRkZGRkZGQBLMhk"
        "ASzIASxkZMhkyGTIZMhkyMhkASzIASxkAAYAZAAAAlgDhAAFAAkADQARABUAGQAAATMRITUhATMRIxMhFSEDMxEjEyEVKQEz"
        "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"
        "ASwQBgAkAAD//wBkAAACWASwECcAbwAAASwQBgAkAAD//wBkAAACWASwECcA2AAAASwQBgAkAAD//wBkAAACvASwECcA3AAA"
        "ASwQBgAkAAD//wBkAAACWARMECcAaAAAAMgQBgAkAAD//wBkAAACWAUUECcA2wAAASwQBgAkAAAAAQAAAAACvAOEABUAABMh"
        "FSERMxUjESEVIREjESMRMxEzESNkAlj+1MjIASz+cMhkZMjIA4Rk/tRk/tRkASz+1AMg/nABkAAABwBk/tQCWAOEAAMABwAP"
        "ABMAFwAbAB8AAAUzFSsBMxUjESEVIxUjNSMlMxUjATMRIxMhFSkBMxUjAZBkZMjIyAEsZGRkASxkZP5wZGRkASz+1AEsZGRk"
        "ZGQBkGRkZMhkArz9RAMgZGQAAAD//wBkAAACWASwECcAQwAAASwQBgAoAAD//wBkAAACWASwECcAbwAAASwQBgAoAAD//wBk"
        "AAACWASwECcA2AAAASwQBgAoAAD//wBkAAACWARMECcAaAAAAMgQBgAoAAD//wBkAAACWASwECcAQwAAASwQBgAsAAD//wBk"
        "AAACWASwECcAbwAAASwQBgAsAAD//wBkAAACWASwECcA2AAAASwQBgAsAAD//wBkAAACWARMECcAaAAAAMgQBgAsAAAAAgAA"
        "AAACWAOEAA8AEwAAEyEVIREzFSMRIRUhESM1MwEzESNkAZD+1MjIASz+cGRkAZBkZAOEZP7UZP7UZAGQZAEs/UQAAAD//wBk"
        "AAACvASwECcA3AAAASwQBgAxAAD//wBkAAACWASwECcAQwAAASwQBgAyAAD//wBkAAACWASwECcAbwAAASwQBgAyAAD//wBk"
        "AAACWASwECcA2AAAASwQBgAyAAD//wBkAAACvASwECcA3AAAASwQBgAyAAD//wBkAAACWARMECcAaAAAAMgQBgAyAAAACQBk"
        "AMgCWAK8AAMABwALAA8AEwAXABsAHwAjAAATMxUjNzMVIzczFSM3MxUjNzMVIxEzFSMnMxUjAzMVIyczFSNkZGRkZGRkZGRk"
        "ZGRkZGRkZGRkZMhkZGRkZAEsZMhkyGTIZMhk/tRkyGQBLGTIZAAAAAAHAAAAAAK8A4QAAwAHAAsAEwAXAB8AIwAAATMVIwEz"
        "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"
        "//8AZAAAAlgEsBAnANkAAAEsEAYAPQAA//8AZAAAAlgD6BAmANkAZBAGAF0AAAAAAAMAyAK8AfQDhAADAAcACwAAEzMVIzcz"
        "FSM7ARUjyGRkZGRkZGRkAyBkyGRkAAAAAAMAyAK8AfQDhAADAAcACwAAATMVKwEzFSMnMxUjAZBkZGRkZGRkZAOEZGTIZAAA"
        "AAMAZAK8AlgDhAADAAcACwAAATMVIykBFSEnMxUjAfRkZP7UASz+1GRkZAOEZGTIZAAAAAAEAMgCvAH0A+gAAwAHAAsADwAA"
        "ATMVKwEzFSMnMxUjNzMVIwGQZGRkZGRkZGRkZGQDhGRkyGTIZAAAAP//AGQCvAK8A4QQBgBhAAD//wDIAZACWAH0EAYAEAAA"
        "AAEAZAGQArwB9AADAAATIRUhZAJY/agB9GQAAAACASwCWAH0A4QAAwAHAAABMxUjEzMVIwEsZGRkZGQDIMgBLGQAAAAAAgDI"
        "AlgBkAOEAAMABwAAEzMVIxMzFSPIZGRkZGQCvGQBLMj//wDI/zgBkABkEAcA4AAA/OAAAP//AMgCWAJYA4QQJgDfZAAQBgDf"
        "nAAAAP//AGQCWAH0A4QQJgDgZAAQBgDgnAAAAP//AGT/OAH0AGQQBwDjAAD84AAAAAEAZAAAAlgDhAALAAABMxUzFSMRIxEj"
        "NTMBLGTIyGTIyAOEyGT9qAJYZAAAAQBkAAACWAOEABMAAAEzFTMVIxEzFSMVIzUjNTMRIzUzASxkyMjIyGTIyMjIA4TIZP7U"
        "ZMjIZAEsZAAAAQEsASwB9AH0AAMAAAEzFSMBLMjIAfTIAAAA//8AZAAAAlgAZBAnABEAyAAAECYAEQAAEAcAEf84AAAAEAAA"
        "AAADIAOEAAMABwALAA8AEwAXABsAHwAjACcAKwAvADMANwA7AD8AAAEzFSsBMxUjETMVKwEzFSsBMxUjAzMVIxMzFSMhMxUj"
        "NzMVIzczFSM3MxUjNzMVIyUzFSsBMxUjAzMVIxMzFSMCvGRkZGRkZGRkZGRkZGRkZGRkZGT+1GRkZGRkZGRkZGRkZGRk/tRk"
        "ZGRkZGRkZGRkZAEsyGQBkGTIZAEsyAEsZGTIZMhkyGTIZMjIZAEsyAEsZAAABQEsAMgCWAK8AAMABwALAA8AEwAAATMVIzcz"
        "FSM3MxUjETMVIyczFSMBLGRkZGRkZGRkZGRkZGQB9GTIZMhk/tRkyGQAAAAABQBkAMgBkAK8AAMABwALAA8AEwAAEzMVIzcz"
        "FSM3MxUjJzMVIyczFSNkZGRkZGRkZGRkZGRkZGQBLGTIZMhkyGTIZAAFAGQAAAK8A4QAEwAXABsAHwAjAAATMxUhFSEVMxUj"
        "FSM1IzUzNSM1MwEzFSMpARUhESEVKQEzFSPIZAEs/tTIyGRkZGRkAZBkZP7UASz+1AEs/tQBLGRkAyDIZGRkyMhkZGT+cGRk"
        "A4RkZAABAAAAAAK8A4QAGQAAEyEVIRUhNTMVMxUjFSM1IRUhFSERIxEjNTNkAZD+1AEsZGRkZP7UASz+1GRkZAOEZGRkZGRk"
        "ZGRk/nACWGQACQAA/5wDIAOEAAMABwALAA8AEwAXABsAHwA3AAABIRUpATMVIwUhFSkBMxEjATMRIwUzFSM3MxUjNzMVIyEz"
        "FTM1MxUzNTMVMxUjFSM1IxUjNSMVIwGQASz+1AEsZGT+1AEs/tQBLGRk/nBkZP7UZGTIZGTIZGT+1GRkZGRkZGRkZGRkZAOE"
        "ZGTIZP7UArz+1MhkZGRkZGRkZGRkZGRkZGRkAAAGAAAAAAK8A4QAAwAHAAsADwATAB0AAAEzFSMTMxEjKQEVIQMzESMTIRUh"
        "FzMVIxUzFSMVIwGQZGTIZGT+DAH0/gxkZGRkAfT+DGTIZGRkZAJYyAGQ/URkAyD9RAMgZGRkyGRkAAAAAAMAAAH0AyAETAAD"
        "AAsAGQAAATMVIxMzESMRIzUzJSEVMxUjESMRIxEjESMB9GRkyGRkZGT9RAGQZGRkZGRkA4TIAZD9qAGQZGRkZP5wAfT+DAH0"
        "AAAAFgEOAAEAAAAAAAAAxQGMAAEAAAAAAAEAEwJ6AAEAAAAAAAIABwKeAAEAAAAAAAMAKAL4AAEAAAAAAAQAEwNJAAEAAAAA"
        "AAUADAN3AAEAAAAAAAYAEQOoAAEAAAAAAAkAHQP2AAEAAAAAAAwALQRwAAEAAAAAAA0AHwTeAAEAAAAAAA4ALgVcAAMAAQQJ"
        "AAABigAAAAMAAQQJAAEAJgJSAAMAAQQJAAIADgKOAAMAAQQJAAMAUAKmAAMAAQQJAAQAJgMhAAMAAQQJAAUAGANdAAMAAQQJ"
        "AAYAIgOEAAMAAQQJAAkAOgO6AAMAAQQJAAwAWgQUAAMAAQQJAA0APgSeAAMAAQQJAA4AXAT+AFIAZQBsAGUAYQBzAGUAZAAg"
        "AGIAeQAgAEoAYQB5AHYAZQBlACAARQBuAGEAZwB1AGEAcwAgACgASABhAHIAdgBlAHQAdABGAG8AeAA5ADYAKQAgADwAaABh"
        "AHIAdgBlAHQAdABmAG8AeAA5ADYAIABbAGEAdABdACAAcAByAG8AdABvAG4AbQBhAGkAbAAgAFsAZABvAHQAXQAgAGMAbwBt"
        "AD4ALAAgAGwAaQBjAGUAbgBzAGUAZAAgAHUAbgBkAGUAcgAgAGEAIABDAHIAZQBhAHQAaQB2AGUAIABDAG8AbQBtAG8AbgBz"
        "ACAAWgBlAHIAbwAgACgAQwBDADAAKQAgADEALgAwACAAPABoAHQAdABwAHMAOgAvAC8AYwByAGUAYQB0AGkAdgBlAGMAbwBt"
        "AG0AbwBuAHMALgBvAHIAZwAvAGwAaQBjAGUAbgBzAGUAcwAvAHoAZQByAG8ALwAxAC4AMAAvAD4ALgAgACgAYwApACAAMgAw"
        "ADAAOQAtADIAMAAxADgALgAAUmVsZWFzZWQgYnkgSmF5dmVlIEVuYWd1YXMgKEhhcnZldHRGb3g5NikgPGhhcnZldHRmb3g5"
        "NiBbYXRdIHByb3Rvbm1haWwgW2RvdF0gY29tPiwgbGljZW5zZWQgdW5kZXIgYSBDcmVhdGl2ZSBDb21tb25zIFplcm8gKEND"
        "MCkgMS4wIDxodHRwczovL2NyZWF0aXZlY29tbW9ucy5vcmcvbGljZW5zZXMvemVyby8xLjAvPi4gKGMpIDIwMDktMjAxOC4A"
        "AFAAaQB4AGUAbAAgAE8AcABlAHIAYQB0AG8AcgAgAE0AbwBuAG8AAFBpeGVsIE9wZXJhdG9yIE1vbm8AAFIAZQBnAHUAbABh"
        "AHIAAFJlZ3VsYXIAAFAAaQB4AGUAbABPAHAAZQByAGEAdABvAHIATQBvAG4AbwA6AEYAbwBuAHQARgBvAHIAZwBlADoAMgAw"
        "ADEAOAAuADEAMAAuADAANAAtADEAAFBpeGVsT3BlcmF0b3JNb25vOkZvbnRGb3JnZToyMDE4LjEwLjA0LTEAAFAAaQB4AGUA"
        "bAAgAE8AcABlAHIAYQB0AG8AcgAgAE0AbwBuAG8AAFBpeGVsIE9wZXJhdG9yIE1vbm8AADIAMAAxADgALgAxADAALgAwADQA"
        "LQAxAAAyMDE4LjEwLjA0LTEAAFAAaQB4AGUAbABPAHAAZQByAGEAdABvAHIATQBvAG4AbwAAUGl4ZWxPcGVyYXRvck1vbm8A"
        "AEoAYQB5AHYAZQBlACAARQBuAGEAZwB1AGEAcwAgACgASABhAHIAdgBlAHQAdABGAG8AeAA5ADYAKQAASmF5dmVlIEVuYWd1"
        "YXMgKEhhcnZldHRGb3g5NikAAG0AYQBpAGwAdABvADoAaABhAHIAdgBlAHQAdABmAG8AeAA5ADYAIABbAGEAdABdACAAcABy"
        "AG8AdABvAG4AbQBhAGkAbAAgAFsAZABvAHQAXQAgAGMAbwBtAABtYWlsdG86aGFydmV0dGZveDk2IFthdF0gcHJvdG9ubWFp"
        "bCBbZG90XSBjb20AAEMAcgBlAGEAdABpAHYAZQAgAEMAbwBtAG0AbwBuAHMAIABaAGUAcgBvACAAKABDAEMAMAApACAAMQAu"
        "ADAAAENyZWF0aXZlIENvbW1vbnMgWmVybyAoQ0MwKSAxLjAAAGgAdAB0AHAAcwA6AC8ALwBjAHIAZQBhAHQAaQB2AGUAYwBv"
        "AG0AbQBvAG4AcwAuAG8AcgBnAC8AbABpAGMAZQBuAHMAZQBzAC8AegBlAHIAbwAvADEALgAwAC8AAGh0dHBzOi8vY3JlYXRp"
        "dmVjb21tb25zLm9yZy9saWNlbnNlcy96ZXJvLzEuMC8AAAAAAAIAAAAAAAD+1ABkAAAAAQAAAAAAAAAAAAAAAAAAAAAA8QAA"
        "AAEAAgADAAQABQAGAAcACAAJAAoACwAMAA0ADgAPABAAEQASABMAFAAVABYAFwAYABkAGgAbABwAHQAeAB8AIAAhACIAIwAk"
        "ACUAJgAnACgAKQAqACsALAAtAC4ALwAwADEAMgAzADQANQA2ADcAOAA5ADoAOwA8AD0APgA/AEAAQQBCAEMARABFAEYARwBI"
        "AEkASgBLAEwATQBOAE8AUABRAFIAUwBUAFUAVgBXAFgAWQBaAFsAXABdAF4AXwBgAGEBAgCjAIQAhQCWAOgAjgCLAKkApACK"
        "AIMAkwCNAJcAiADDAN4AqgCiAK0AyQDHAK4AYgBjAJAAZADLAGUAyADKAM8AzADNAM4A6QBmANMA0ADRAK8AZwDwAJEA1gDU"
        "ANUAaADrAO0AiQBqAGkAawBtAGwAbgCgAG8AcQBwAHIAcwB1AHQAdgB3AOoAeAB6AHkAewB9AHwAuAChAH8AfgCAAIEA7ADu"
        "ALoBAwEEAP8BAAEFAQYBBwEIAQkBCgELAQwA1wENAQ4BDwEQALAAsQERARIBEwEUAOQA5QEVARYBFwEYARkBGgC7AOYA5wDY"
        "AOEA2wDdANkAsgCzALYAtwDEALQAtQDFAIIAwgCHAKsAxgC+AL8BGwEcAR0BHgCMB3VuaTAwQTALQ2NpcmN1bWZsZXgLY2Np"
        "cmN1bWZsZXgGRGNhcm9uBmRjYXJvbgZFY2Fyb24GZWNhcm9uC0djaXJjdW1mbGV4C2djaXJjdW1mbGV4C0hjaXJjdW1mbGV4"
        "C2hjaXJjdW1mbGV4C0pjaXJjdW1mbGV4C2pjaXJjdW1mbGV4Bk5jYXJvbgZuY2Fyb24GUmNhcm9uBnJjYXJvbgtTY2lyY3Vt"
        "ZmxleAtzY2lyY3VtZmxleAZUY2Fyb24GdGNhcm9uBlVicmV2ZQZ1YnJldmUFVXJpbmcFdXJpbmcERXVybwd1bmkyMEIxB3Vu"
        "aTIwQjcHdW5pMjExNwAAAAAAAAH//wACAAAAAQAAAADU9Tw7AAAAAMqcmWMAAAAA19tERA=="
    ),
}

_EMBEDDED_FONT_LICENSES = 'Fonts used by the Crafty looks of Bedrock Server Manager (unpacked from the program file).\n\n=== Silkscreen (Silkscreen-Bold.ttf) — SIL Open Font License 1.1 ===\nCopyright 2001 The Silkscreen Project Authors (https://github.com/googlefonts/silkscreen)\n\nThis Font Software is licensed under the SIL Open Font License, Version 1.1.\nThis license is copied below, and is also available with a FAQ at:\nhttps://scripts.sil.org/OFL\n\n\n-----------------------------------------------------------\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n-----------------------------------------------------------\n\nPREAMBLE\nThe goals of the Open Font License (OFL) are to stimulate worldwide\ndevelopment of collaborative font projects, to support the font creation\nefforts of academic and linguistic communities, and to provide a free and\nopen framework in which fonts may be shared and improved in partnership\nwith others.\n\nThe OFL allows the licensed fonts to be used, studied, modified and\nredistributed freely as long as they are not sold by themselves. The\nfonts, including any derivative works, can be bundled, embedded, \nredistributed and/or sold with any software provided that any reserved\nnames are not used by derivative works. The fonts and derivatives,\nhowever, cannot be released under any other type of license. The\nrequirement for fonts to remain under this license does not apply\nto any document created using the fonts or their derivatives.\n\nDEFINITIONS\n"Font Software" refers to the set of files released by the Copyright\nHolder(s) under this license and clearly marked as such. This may\ninclude source files, build scripts and documentation.\n\n"Reserved Font Name" refers to any names specified as such after the\ncopyright statement(s).\n\n"Original Version" refers to the collection of Font Software components as\ndistributed by the Copyright Holder(s).\n\n"Modified Version" refers to any derivative made by adding to, deleting,\nor substituting -- in part or in whole -- any of the components of the\nOriginal Version, by changing formats or by porting the Font Software to a\nnew environment.\n\n"Author" refers to any designer, engineer, programmer, technical\nwriter or other person who contributed to the Font Software.\n\nPERMISSION & CONDITIONS\nPermission is hereby granted, free of charge, to any person obtaining\na copy of the Font Software, to use, study, copy, merge, embed, modify,\nredistribute, and sell modified and unmodified copies of the Font\nSoftware, subject to the following conditions:\n\n1) Neither the Font Software nor any of its individual components,\nin Original or Modified Versions, may be sold by itself.\n\n2) Original or Modified Versions of the Font Software may be bundled,\nredistributed and/or sold with any software, provided that each copy\ncontains the above copyright notice and this license. These can be\nincluded either as stand-alone text files, human-readable headers or\nin the appropriate machine-readable metadata fields within text or\nbinary files as long as those fields can be easily viewed by the user.\n\n3) No Modified Version of the Font Software may use the Reserved Font\nName(s) unless explicit written permission is granted by the corresponding\nCopyright Holder. This restriction only applies to the primary font name as\npresented to the users.\n\n4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font\nSoftware shall not be used to promote, endorse or advertise any\nModified Version, except to acknowledge the contribution(s) of the\nCopyright Holder(s) and the Author(s) or with their explicit written\npermission.\n\n5) The Font Software, modified or unmodified, in part or in whole,\nmust be distributed entirely under this license, and must not be\ndistributed under any other license. The requirement for fonts to\nremain under this license does not apply to any document created\nusing the Font Software.\n\nTERMINATION\nThis license becomes null and void if any of the above conditions are\nnot met.\n\nDISCLAIMER\nTHE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,\nEXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF\nMERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT\nOF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE\nCOPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,\nINCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL\nDAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING\nFROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM\nOTHER DEALINGS IN THE FONT SOFTWARE.\n\n\n=== Pixelify Sans (PixelifySans.ttf) — SIL Open Font License 1.1 ===\nCopyright 2021 The Pixelify Sans Project Authors (https://github.com/eifetx/Pixelify-Sans)\n\nThis Font Software is licensed under the SIL Open Font License, Version 1.1.\nThis license is copied below, and is also available with a FAQ at:\nhttps://scripts.sil.org/OFL\n\n\n-----------------------------------------------------------\nSIL OPEN FONT LICENSE Version 1.1 - 26 February 2007\n-----------------------------------------------------------\n\nPREAMBLE\nThe goals of the Open Font License (OFL) are to stimulate worldwide\ndevelopment of collaborative font projects, to support the font creation\nefforts of academic and linguistic communities, and to provide a free and\nopen framework in which fonts may be shared and improved in partnership\nwith others.\n\nThe OFL allows the licensed fonts to be used, studied, modified and\nredistributed freely as long as they are not sold by themselves. The\nfonts, including any derivative works, can be bundled, embedded, \nredistributed and/or sold with any software provided that any reserved\nnames are not used by derivative works. The fonts and derivatives,\nhowever, cannot be released under any other type of license. The\nrequirement for fonts to remain under this license does not apply\nto any document created using the fonts or their derivatives.\n\nDEFINITIONS\n"Font Software" refers to the set of files released by the Copyright\nHolder(s) under this license and clearly marked as such. This may\ninclude source files, build scripts and documentation.\n\n"Reserved Font Name" refers to any names specified as such after the\ncopyright statement(s).\n\n"Original Version" refers to the collection of Font Software components as\ndistributed by the Copyright Holder(s).\n\n"Modified Version" refers to any derivative made by adding to, deleting,\nor substituting -- in part or in whole -- any of the components of the\nOriginal Version, by changing formats or by porting the Font Software to a\nnew environment.\n\n"Author" refers to any designer, engineer, programmer, technical\nwriter or other person who contributed to the Font Software.\n\nPERMISSION & CONDITIONS\nPermission is hereby granted, free of charge, to any person obtaining\na copy of the Font Software, to use, study, copy, merge, embed, modify,\nredistribute, and sell modified and unmodified copies of the Font\nSoftware, subject to the following conditions:\n\n1) Neither the Font Software nor any of its individual components,\nin Original or Modified Versions, may be sold by itself.\n\n2) Original or Modified Versions of the Font Software may be bundled,\nredistributed and/or sold with any software, provided that each copy\ncontains the above copyright notice and this license. These can be\nincluded either as stand-alone text files, human-readable headers or\nin the appropriate machine-readable metadata fields within text or\nbinary files as long as those fields can be easily viewed by the user.\n\n3) No Modified Version of the Font Software may use the Reserved Font\nName(s) unless explicit written permission is granted by the corresponding\nCopyright Holder. This restriction only applies to the primary font name as\npresented to the users.\n\n4) The name(s) of the Copyright Holder(s) or the Author(s) of the Font\nSoftware shall not be used to promote, endorse or advertise any\nModified Version, except to acknowledge the contribution(s) of the\nCopyright Holder(s) and the Author(s) or with their explicit written\npermission.\n\n5) The Font Software, modified or unmodified, in part or in whole,\nmust be distributed entirely under this license, and must not be\ndistributed under any other license. The requirement for fonts to\nremain under this license does not apply to any document created\nusing the Font Software.\n\nTERMINATION\nThis license becomes null and void if any of the above conditions are\nnot met.\n\nDISCLAIMER\nTHE FONT SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,\nEXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO ANY WARRANTIES OF\nMERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT\nOF COPYRIGHT, PATENT, TRADEMARK, OR OTHER RIGHT. IN NO EVENT SHALL THE\nCOPYRIGHT HOLDER BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,\nINCLUDING ANY GENERAL, SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL\nDAMAGES, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING\nFROM, OUT OF THE USE OR INABILITY TO USE THE FONT SOFTWARE OR FROM\nOTHER DEALINGS IN THE FONT SOFTWARE.\n\n\n=== Pixel Operator Mono (PixelOperatorMono.ttf) by Jayvee Enaguas (HarvettFox96) — CC0 1.0 ===\nCreative Commons Legal Code\n\nCC0 1.0 Universal\n\n    CREATIVE COMMONS CORPORATION IS NOT A LAW FIRM AND DOES NOT PROVIDE\n    LEGAL SERVICES. DISTRIBUTION OF THIS DOCUMENT DOES NOT CREATE AN\n    ATTORNEY-CLIENT RELATIONSHIP. CREATIVE COMMONS PROVIDES THIS\n    INFORMATION ON AN "AS-IS" BASIS. CREATIVE COMMONS MAKES NO WARRANTIES\n    REGARDING THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS\n    PROVIDED HEREUNDER, AND DISCLAIMS LIABILITY FOR DAMAGES RESULTING FROM\n    THE USE OF THIS DOCUMENT OR THE INFORMATION OR WORKS PROVIDED\n    HEREUNDER.\n\nStatement of Purpose\n\nThe laws of most jurisdictions throughout the world automatically confer\nexclusive Copyright and Related Rights (defined below) upon the creator\nand subsequent owner(s) (each and all, an "owner") of an original work of\nauthorship and/or a database (each, a "Work").\n\nCertain owners wish to permanently relinquish those rights to a Work for\nthe purpose of contributing to a commons of creative, cultural and\nscientific works ("Commons") that the public can reliably and without fear\nof later claims of infringement build upon, modify, incorporate in other\nworks, reuse and redistribute as freely as possible in any form whatsoever\nand for any purposes, including without limitation commercial purposes.\nThese owners may contribute to the Commons to promote the ideal of a free\nculture and the further production of creative, cultural and scientific\nworks, or to gain reputation or greater distribution for their Work in\npart through the use and efforts of others.\n\nFor these and/or other purposes and motivations, and without any\nexpectation of additional consideration or compensation, the person\nassociating CC0 with a Work (the "Affirmer"), to the extent that he or she\nis an owner of Copyright and Related Rights in the Work, voluntarily\nelects to apply CC0 to the Work and publicly distribute the Work under its\nterms, with knowledge of his or her Copyright and Related Rights in the\nWork and the meaning and intended legal effect of CC0 on those rights.\n\n1. Copyright and Related Rights. A Work made available under CC0 may be\nprotected by copyright and related or neighboring rights ("Copyright and\nRelated Rights"). Copyright and Related Rights include, but are not\nlimited to, the following:\n\n  i. the right to reproduce, adapt, distribute, perform, display,\n     communicate, and translate a Work;\n ii. moral rights retained by the original author(s) and/or performer(s);\niii. publicity and privacy rights pertaining to a person\'s image or\n     likeness depicted in a Work;\n iv. rights protecting against unfair competition in regards to a Work,\n     subject to the limitations in paragraph 4(a), below;\n  v. rights protecting the extraction, dissemination, use and reuse of data\n     in a Work;\n vi. database rights (such as those arising under Directive 96/9/EC of the\n     European Parliament and of the Council of 11 March 1996 on the legal\n     protection of databases, and under any national implementation\n     thereof, including any amended or successor version of such\n     directive); and\nvii. other similar, equivalent or corresponding rights throughout the\n     world based on applicable law or treaty, and any national\n     implementations thereof.\n\n2. Waiver. To the greatest extent permitted by, but not in contravention\nof, applicable law, Affirmer hereby overtly, fully, permanently,\nirrevocably and unconditionally waives, abandons, and surrenders all of\nAffirmer\'s Copyright and Related Rights and associated claims and causes\nof action, whether now known or unknown (including existing as well as\nfuture claims and causes of action), in the Work (i) in all territories\nworldwide, (ii) for the maximum duration provided by applicable law or\ntreaty (including future time extensions), (iii) in any current or future\nmedium and for any number of copies, and (iv) for any purpose whatsoever,\nincluding without limitation commercial, advertising or promotional\npurposes (the "Waiver"). Affirmer makes the Waiver for the benefit of each\nmember of the public at large and to the detriment of Affirmer\'s heirs and\nsuccessors, fully intending that such Waiver shall not be subject to\nrevocation, rescission, cancellation, termination, or any other legal or\nequitable action to disrupt the quiet enjoyment of the Work by the public\nas contemplated by Affirmer\'s express Statement of Purpose.\n\n3. Public License Fallback. Should any part of the Waiver for any reason\nbe judged legally invalid or ineffective under applicable law, then the\nWaiver shall be preserved to the maximum extent permitted taking into\naccount Affirmer\'s express Statement of Purpose. In addition, to the\nextent the Waiver is so judged Affirmer hereby grants to each affected\nperson a royalty-free, non transferable, non sublicensable, non exclusive,\nirrevocable and unconditional license to exercise Affirmer\'s Copyright and\nRelated Rights in the Work (i) in all territories worldwide, (ii) for the\nmaximum duration provided by applicable law or treaty (including future\ntime extensions), (iii) in any current or future medium and for any number\nof copies, and (iv) for any purpose whatsoever, including without\nlimitation commercial, advertising or promotional purposes (the\n"License"). The License shall be deemed effective as of the date CC0 was\napplied by Affirmer to the Work. Should any part of the License for any\nreason be judged legally invalid or ineffective under applicable law, such\npartial invalidity or ineffectiveness shall not invalidate the remainder\nof the License, and in such case Affirmer hereby affirms that he or she\nwill not (i) exercise any of his or her remaining Copyright and Related\nRights in the Work or (ii) assert any associated claims and causes of\naction with respect to the Work, in either case contrary to Affirmer\'s\nexpress Statement of Purpose.\n\n4. Limitations and Disclaimers.\n\n a. No trademark or patent rights held by Affirmer are waived, abandoned,\n    surrendered, licensed or otherwise affected by this document.\n b. Affirmer offers the Work as-is and makes no representations or\n    warranties of any kind concerning the Work, express, implied,\n    statutory or otherwise, including without limitation warranties of\n    title, merchantability, fitness for a particular purpose, non\n    infringement, or the absence of latent or other defects, accuracy, or\n    the present or absence of errors, whether or not discoverable, all to\n    the greatest extent permissible under applicable law.\n c. Affirmer disclaims responsibility for clearing rights of other persons\n    that may apply to the Work or any use thereof, including without\n    limitation any person\'s Copyright and Related Rights in the Work.\n    Further, Affirmer disclaims responsibility for obtaining any necessary\n    consents, permissions or other rights required for any use of the\n    Work.\n d. Affirmer understands and acknowledges that Creative Commons is not a\n    party to this document and has no duty or obligation with respect to\n    this CC0 or use of the Work.\n'


if __name__ == "__main__":
    main()
