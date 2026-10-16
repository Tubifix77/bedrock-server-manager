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
APP_VERSION = "3.1"
# Release name, shown next to the version everywhere a user sees it (window
# title, About, Machine page). The wire protocol / app_version fields keep the
# plain APP_VERSION.
APP_RELEASE_NAME = "Servers"
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
        self.setup_styles()
        self.setup_ui()
        self.apply_theme()
        self.load_saved_state()
        self.setup_keyboard_shortcuts()
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
        top_bar = ttk.Frame(self.root)
        top_bar.pack(fill=tk.X, side=tk.TOP, padx=5, pady=(5, 0))
        self.sidebar_toggle_btn = ttk.Button(top_bar, command=self.toggle_sidebar)
        self.sidebar_toggle_btn.pack(side=tk.LEFT)
        # Server strip (3.0, docs/V3-SERVERS-PLAN.md §1): one button per Server
        # of the selected Machine, "+"/"−" pinned right. "Server N" is display-
        # only: N is the position in the list, never stored anywhere.
        self.server_remove_btn = ttk.Button(top_bar, text="−", width=3, command=self.remove_server_dialog)
        self.server_remove_btn.pack(side=tk.RIGHT)
        self.server_add_btn = ttk.Button(top_bar, text="+", width=3, command=self.new_server_dialog)
        self.server_add_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self.server_rename_btn = ttk.Button(top_bar, text="✏", width=3, command=self.rename_server_dialog)
        self.server_rename_btn.pack(side=tk.RIGHT, padx=(0, 2))
        self.create_tooltip(self.server_rename_btn, "Rename the selected Server")
        self.server_strip = ttk.Frame(top_bar)
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
        if not self._sidebar_collapsed:
            self.main_pane.add(self.sidebar_frame, weight=0)
        self._update_sidebar_toggle_btn()

        notebook_frame = ttk.Frame(self.main_pane)
        self.main_pane.add(notebook_frame, weight=1)

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
        # Heading over the whole tree, which lists ALL Machines (this computer +
        # any paired remote ones). The "This computer" *node* inside the tree is
        # a separate thing -- one Machine among possibly several.
        ttk.Label(parent, text="🖥 Machines", font=("TkDefaultFont", 9, "bold")).pack(
            anchor="w", padx=6, pady=(6, 2))
        self.sidebar_tree = ttk.Treeview(parent, show="tree", height=15)
        self.sidebar_tree.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        self.sidebar_tree.bind("<<TreeviewSelect>>", self.on_sidebar_select)
        btns = ttk.Frame(parent)
        btns.pack(fill=tk.X, padx=4, pady=(0, 6))
        # Adding a Server is the top line's "+" since 3.0.
        ttk.Button(btns, text="➕ Machine", command=self.add_machine_dialog).pack(fill=tk.X)

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
            b = ttk.Radiobutton(self.server_strip, text=self._strip_label(e), value=e["key"],
                                variable=self._strip_choice, style="Toolbutton",
                                command=lambda k=e["key"]: self._on_strip_select(k))
            tip = self._strip_label(e, short=False)
            if e["online"] is not None:
                tip += f"\n{e['online']} player{'s' if e['online'] != 1 else ''} online"
            self.create_tooltip(b, tip)
            buttons.append((e, b))
        # Fit as many as the width allows; the rest go into "▾ N more". The
        # selected Server always stays visible (it takes the last visible slot).
        avail = self.server_strip.winfo_width()
        more = ttk.Menubutton(self.server_strip, text="▾ 99 more")
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
        ttk.Label(body, text=f"New Server (Server {number})", font=("TkDefaultFont", 12, "bold")).grid(
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
            seed_label.config(foreground="" if new else "gray")
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
        lan_box = ttk.Checkbutton(body, text="Show in Minecraft's LAN list", variable=lan_var)
        lan_box.grid(row=4, column=1, sticky="w", pady=(6, 0))
        ttk.Label(body, text="\n".join("• " + n for n in notes), foreground="gray", wraplength=460,
                  justify=tk.LEFT).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 6))
        status = ttk.Label(body, text="", foreground="gray", wraplength=460, justify=tk.LEFT)
        status.grid(row=6, column=0, columnspan=2, sticky="w")
        bar = ttk.Progressbar(body, maximum=100)
        bar.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 8))

        btns = ttk.Frame(body); btns.grid(row=8, column=0, columnspan=2, sticky="ew")
        existing = ttk.Button(btns, text="Use an existing Bedrock folder instead…", style="Small.TButton",
                              command=lambda: (dlg.destroy(), self.add_server_profile()))
        existing.pack(side=tk.LEFT)
        cancel = ttk.Button(btns, text="Cancel", command=dlg.destroy)
        cancel.pack(side=tk.RIGHT)
        create = ttk.Button(btns, text="Create Server", style="Primary.TButton")
        create.pack(side=tk.RIGHT, padx=(0, 6))

        def set_status(text, pct=None, error=False):
            status.config(text=text, foreground="#C62828" if error else "gray")
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
                    tk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 10))
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
        ttk.Label(body, text=f"Rename Server {number}", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(body, text="New name:").grid(row=1, column=0, sticky="e", padx=(0, 6))
        name_var = tk.StringVar(value=old_world)
        entry = ttk.Entry(body, textvariable=name_var, width=34)
        entry.grid(row=1, column=1, sticky="w")
        # Follow along by default only while players still see the stock name or
        # the old one -- a server-name the user chose on purpose is left alone.
        players_var = tk.BooleanVar(value=old_players_name.strip() in ("", "Dedicated Server", old_world,
                                                                      profile.get("name", "")))
        ttk.Checkbutton(body, variable=players_var,
                        text=f"Also the name players see in their server list (now: “{old_players_name or '—'}”)").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        status = ttk.Label(body, text="", foreground="#E65100", wraplength=420, justify=tk.LEFT)
        status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        rename_btn = ttk.Button(btns, text="Rename", style="Primary.TButton")
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
                status.config(text="Use a name without ; / \\ : * ? \" < > |", foreground="#C62828")
                return
            if acc.is_running():
                status.config(text="Stop this Server first — its World folder is renamed too.", foreground="#E65100")
                return
            try:
                self._rename_server(pid, acc, old_world, new, players_var.get())
            except Exception as e:
                status.config(text=f"Couldn't rename it: {e}", foreground="#C62828")
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
                      font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0, 8))
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
            ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
            nxt = ttk.Button(row, text="Next…", state=tk.DISABLED, command=confirm_page)
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
            ttk.Label(body, text=f"Permanently delete {title}?", font=("TkDefaultFont", 12, "bold"),
                      foreground="#C62828").pack(anchor="w", pady=(0, 8))
            lines = [f"Its folder: {path}",
                     f"Worlds: {', '.join(worlds) if worlds else '(none yet)'}",
                     f"Backups: {n_backups} (in {bdir})"]
            ttk.Label(body, text="This deletes, and it can't be undone:\n" + "\n".join("   • " + l for l in lines),
                      justify=tk.LEFT, wraplength=520).pack(anchor="w")
            if ctx and ctx.is_running():
                ttk.Label(body, text="It's running and will be stopped first.", foreground="#E65100").pack(anchor="w", pady=(6, 0))
            ttk.Label(body, text=f"\nType  {e['world']}  to confirm:").pack(anchor="w")
            typed = tk.StringVar()
            entry = ttk.Entry(body, textvariable=typed, width=40); entry.pack(anchor="w", pady=(2, 8))
            status = ttk.Label(body, text="", foreground="gray", wraplength=520, justify=tk.LEFT)
            status.pack(anchor="w")
            row = ttk.Frame(body); row.pack(fill=tk.X, pady=(8, 0))
            ttk.Button(row, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
            delete = ttk.Button(row, text="Delete permanently", style="Danger.TButton", state=tk.DISABLED,
                                command=lambda: self._remove_server_async(dlg, status, pid, e))
            delete.pack(side=tk.RIGHT, padx=(0, 6))
            ttk.Button(row, text="◀ Back", command=pick_page).pack(side=tk.LEFT)
            typed.trace_add("write", lambda *a: delete.config(
                state=tk.NORMAL if typed.get().strip() == e["world"] else tk.DISABLED))
            entry.focus_set()

        pick_page()
        dlg.bind("<Escape>", lambda ev: dlg.destroy())
        dlg.grab_set()

    def _remove_server_async(self, dlg, status, pid: str, entry: dict):
        ctx = self.contexts.get(pid)
        if ctx and ctx.online_players():  # someone joined meanwhile
            status.config(text="Players are online now — try again when they've left.", foreground="#C62828")
            return
        for w in dlg.winfo_children():
            for c in w.winfo_children():
                try:
                    c.config(state=tk.DISABLED)
                except tk.TclError:
                    pass
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)
        path = Path(self.config["server_profiles"][pid]["path"])
        status.config(text="Stopping…" if ctx and ctx.is_running() else "Deleting…", foreground="gray")

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
                           font=("TkDefaultFont", 8), foreground="gray")
        status.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(6, 4))

        def do_test(save_after=False):
            host = fields["host"].get().strip()
            token = fields["token"].get().strip()
            try:
                port = int(fields["port"].get())
            except ValueError:
                status.config(text="Port must be a number.", foreground="#F44336")
                return
            if not host or not token:
                status.config(text="Host and token are required.", foreground="#F44336")
                return
            status.config(text="Testing…", foreground="gray")
            dlg.update_idletasks()

            def worker():
                try:
                    conn, info = remote_connect(host, port, token, timeout=6)
                    conn.close()
                    self.root.after(0, lambda: on_ok(info))
                except Exception as e:
                    self.root.after(0, lambda e=e: status.config(text=f"✗ {e}", foreground="#F44336"))

            def on_ok(info):
                status.config(text=f"✓ Connected to {info.get('name', host)} "
                                   f"({len(info.get('servers', []))} Server(s)).", foreground="#4CAF50")
                if save_after:
                    m = self.add_machine(fields["name"].get(), host, port, token)
                    self._start_connection(m)
                    self.refresh_sidebar()
                    dlg.destroy()
            threading.Thread(target=worker, daemon=True).start()

        btns = ttk.Frame(frm)
        btns.grid(row=len(rows) + 1, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btns, text="Test connection", command=lambda: do_test(False)).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Save", command=lambda: do_test(True), style="Primary.TButton").pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.LEFT, padx=3)

    # ==================================================================
    # Fleet overview + Machine page (Stage 4a) -- shown INSTEAD of the
    # notebook when the sidebar's Fleet root or a Machine node is selected.
    # ==================================================================
    def show_notebook(self):
        self.overview_frame.pack_forget()
        self.notebook.pack(fill=tk.BOTH, expand=True)

    def show_overview(self):
        self.notebook.pack_forget()
        self.overview_frame.pack(fill=tk.BOTH, expand=True)

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
        ttk.Label(self.overview_frame, text="🌐 Fleet — every Server, every Machine",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(self.overview_frame, text="Double-click a row to open it; select + use the buttons to start/stop.",
                  font=("TkDefaultFont", 8), foreground="gray").pack(anchor="w", pady=(0, 4))
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
        ttk.Label(header, text=title, font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(header, text=subtitle, foreground="gray").pack(anchor="w")

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
        ttk.Button(btns, text="▶️ Start Selected", command=self.fleet_start_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(btns, text="⏹️ Stop Selected", command=self.fleet_stop_selected).pack(side=tk.LEFT, padx=3)
        if machine_id is not None:
            ttk.Button(btns, text="🔌 Remove Machine",
                       command=lambda: self.remove_machine_confirm(machine_id)).pack(side=tk.RIGHT, padx=3)
        ttk.Button(btns, text="🔄 Refresh", command=lambda: self.refresh_sidebar()).pack(side=tk.RIGHT, padx=3)

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
        self.main_tab.columnconfigure(0, weight=1)
        
        header = ttk.Frame(self.main_tab)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Update Bedrock Server", font=("TkDefaultFont", 14, "bold")).pack(side=tk.LEFT)

        # The app asks Mojang for the newest release itself (fetch_latest_bedrock_release),
        # so updating is one button -- no Wiki lookup, typing or ZIP hunting.
        version_frame = ttk.LabelFrame(self.main_tab, text="Bedrock Server Version", padding=10)
        version_frame.grid(row=1, column=0, sticky="ew", pady=5)
        version_frame.columnconfigure(0, weight=1)
        self.update_version_label = ttk.Label(version_frame, text="Installed: —   ·   Latest: —",
                                              font=("TkDefaultFont", 11))
        self.update_version_label.grid(row=0, column=0, sticky="w")
        self.update_state_label = ttk.Label(version_frame, text="", foreground="gray")
        self.update_state_label.grid(row=1, column=0, sticky="w", pady=(2, 6))
        actions = ttk.Frame(version_frame)
        actions.grid(row=2, column=0, sticky="w")
        self.update_button = ttk.Button(actions, text="Update", command=self.start_update, style="Primary.TButton")
        self.update_button.pack(side=tk.LEFT)
        # Only shown with two or more Servers on this Machine (_refresh_update_button).
        self.update_all_button = ttk.Button(actions, text="Update all Servers", command=self.update_all_servers)
        ttk.Button(actions, text="🔄 Check again", style="Small.TButton",
                   command=lambda: self.refresh_update_status(force=True)).pack(side=tk.LEFT, padx=(8, 0))
        self.advanced_toggle = ttk.Button(actions, text="Advanced ▸", style="Small.TButton",
                                          command=self._toggle_update_advanced)
        self.advanced_toggle.pack(side=tk.LEFT, padx=(8, 0))

        # Advanced: a specific ZIP or a manually entered version. Only needed if
        # the automatic check can't reach Mojang (it opens by itself then).
        self.advanced_frame = ttk.Frame(version_frame)
        self.advanced_frame.columnconfigure(1, weight=1)
        ttk.Label(self.advanced_frame, text="Bedrock Server ZIP:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.zip_entry = ttk.Entry(self.advanced_frame)
        self.zip_entry.grid(row=0, column=1, sticky="ew", padx=5)
        self.zip_entry.bind("<KeyRelease>", self.validate_inputs)
        ttk.Button(self.advanced_frame, text="Browse", command=self.browse_zip).grid(row=0, column=2, padx=5)
        ttk.Button(self.advanced_frame, text="Enter a version…", command=self.manual_version_input).grid(row=0, column=3, padx=5)
        self.zip_status = ttk.Label(self.advanced_frame, text="", foreground="gray")
        self.zip_status.grid(row=1, column=1, sticky="w", padx=5)

        # Per-Server update policy (docs/V2-MAJORDOMO-PLAN.md) -- lives here,
        # not the app-level Settings tab, since it's specific to this Server.
        update_settings_frame = ttk.LabelFrame(self.main_tab, text="Update Settings (this Server)", padding=10)
        update_settings_frame.grid(row=2, column=0, sticky="ew", pady=5)
        self.auto_stop_var = tk.BooleanVar(value=self.config.get("auto_stop_server_before_update", True))
        ttk.Checkbutton(update_settings_frame, text="Automatically stop server before update", variable=self.auto_stop_var).grid(row=0, column=0, sticky="w", pady=2)
        self.auto_start_var = tk.BooleanVar(value=self.config.get("auto_start_server_after_update", False))
        ttk.Checkbutton(update_settings_frame, text="Automatically start server after update", variable=self.auto_start_var).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Button(update_settings_frame, text="💾 Save", command=self.save_settings).grid(row=2, column=0, sticky="w", pady=(6, 0))

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
        self.log("Application started. (First time? Press + at the top right to create your first Server.)", "info")
    
    def setup_server_tab(self):
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
        ttk.Button(tp_frame, text="Go", command=self.teleport_player).pack(side=tk.LEFT, padx=(6, 0))
        self.tp_feedback = ttk.Label(tp_frame, text="", font=("TkDefaultFont", 10, "bold"))
        self.tp_feedback.pack(side=tk.LEFT, padx=(10, 0))
        self._reply_slot("teleport", self.tp_feedback)
    
    def setup_backup_tab(self):
        self.backup_tab.columnconfigure(0, weight=1)
        self.backup_tab.rowconfigure(4, weight=1)
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
        # Backup policy is per Server (docs/V2-MAJORDOMO-PLAN.md), so it lives
        # here beside the preserve list rather than in the app-level Settings tab.
        backup_settings_frame = ttk.LabelFrame(self.backup_tab, text="Backup Settings (this Server)", padding=10)
        backup_settings_frame.grid(row=3, column=0, sticky="ew", pady=5)
        ttk.Label(backup_settings_frame, text="Maximum backups to keep:").grid(row=0, column=0, sticky="w", pady=5)
        self.max_backups_var = tk.IntVar(value=self.config.get("max_backups", 5))
        ttk.Spinbox(backup_settings_frame, from_=1, to=50, width=10, textvariable=self.max_backups_var).grid(row=0, column=1, sticky="w", padx=10)
        self.auto_cleanup_var = tk.BooleanVar(value=self.config.get("auto_cleanup_backups", True))
        ttk.Checkbutton(backup_settings_frame, text="Automatically cleanup old backups after update", variable=self.auto_cleanup_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.compress_var = tk.BooleanVar(value=self.config.get("compress_backups", False))
        ttk.Checkbutton(backup_settings_frame, text="Compress backups (ZIP format, slower but smaller)", variable=self.compress_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Button(backup_settings_frame, text="💾 Save", command=self.save_settings).grid(row=3, column=0, sticky="w", pady=(6, 0))
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
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side=tk.RIGHT)
        ttk.Button(btns, text="Create", style="Primary.TButton", command=ok).pack(side=tk.RIGHT, padx=(0, 6))
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
        # The Server's own answer to the last button press (_check_console_replies):
        # the console that shows it lives on another tab.
        self.gm_feedback = ttk.Label(gm, text="", font=("TkDefaultFont", 10, "bold"))
        self.gm_feedback.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(gm, text="Applies live to an online player and sticks for them afterwards (Server must be running).",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))
        self.gm_force_warn = ttk.Label(gm, text="", font=("TkDefaultFont", 8), foreground="#F44336")
        self.gm_force_warn.grid(row=3, column=0, columnspan=5, sticky="w")
        self._reply_slot("gamemode", self.gm_feedback)
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
        acc = self.active_access
        a, b = self.tp_from.get().strip(), self.tp_to.get().strip()
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
                          "No answer from the Server — see the console above.")

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
            slot["label"].config(text=text, foreground=colour)
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
        ttk.Button(location_frame, text="Browse", command=self.browse_server).grid(row=0, column=2, padx=5)
        self.server_status = ttk.Label(location_frame, text="", foreground="gray")
        self.server_status.grid(row=1, column=1, sticky="w", padx=5)
        ttk.Label(location_frame, text="Relocates the selected Server's folder. To add another Server, use + at the top right.",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=2, column=1, sticky="w", padx=5)
        ui_frame = ttk.LabelFrame(self.settings_tab, text="Interface Settings", padding=10)
        ui_frame.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Label(ui_frame, text="Console font size:").grid(row=0, column=0, sticky="w", pady=5)
        self.font_size_var = tk.IntVar(value=self.config.get("console_font_size", 9))
        ttk.Spinbox(ui_frame, from_=6, to=24, width=10, textvariable=self.font_size_var).grid(row=0, column=1, sticky="w", padx=10)
        self.notifications_var = tk.BooleanVar(value=self.config.get("show_notifications", True))
        ttk.Checkbutton(ui_frame, text="Show notification messages", variable=self.notifications_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=5)
        self.dark_mode_var = tk.BooleanVar(value=self.config.get("dark_mode", False))
        ttk.Checkbutton(ui_frame, text="🌙 Dark mode", variable=self.dark_mode_var, command=self.toggle_dark_mode).grid(row=2, column=0, columnspan=2, sticky="w", pady=5)
        self.check_updates_var = tk.BooleanVar(value=self.config.get("check_updates_on_start", True))
        ttk.Checkbutton(ui_frame, text="Check for server updates on application start", variable=self.check_updates_var).grid(row=3, column=0, columnspan=2, sticky="w", pady=5)

        # --- Remote Administration: let another computer running this app
        #     manage THIS machine's Servers over the LAN (docs/V2-MAJORDOMO-PLAN.md).
        ra_cfg = self.config.setdefault("remote_admin", {"enabled": False, "port": REMOTE_DEFAULT_PORT, "token": ""})
        ra_frame = ttk.LabelFrame(self.settings_tab, text="Remote Administration (this Machine)", padding=10)
        ra_frame.grid(row=2, column=0, sticky="ew", pady=5)
        ra_frame.columnconfigure(1, weight=1)
        self.remote_enabled_var = tk.BooleanVar(value=bool(ra_cfg.get("enabled")))
        ttk.Checkbutton(ra_frame, text="Allow this computer to be administered from another PC on the network",
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
        ttk.Button(tbtns, text="📋 Copy", command=self.copy_remote_token, width=8).pack(side=tk.LEFT, padx=2)
        ttk.Button(tbtns, text="🔄 New", command=self.regenerate_remote_token, width=6).pack(side=tk.LEFT, padx=2)
        self.remote_status_label = ttk.Label(ra_frame, text="", font=("TkDefaultFont", 8), foreground="gray")
        self.remote_status_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(ra_frame,
                  text="LAN only — enter this machine's address + token on the other PC's ➕ Machine. Don't port-forward this.",
                  font=("TkDefaultFont", 8), foreground="gray").grid(row=4, column=0, columnspan=3, sticky="w")

        btn_frame = ttk.Frame(self.settings_tab)
        btn_frame.grid(row=3, column=0, sticky="ew", pady=20)
        ttk.Button(btn_frame, text="💾 Save Settings", command=self.save_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="🔄 Reset to Defaults", command=self.reset_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="📂 Open Config File", command=self.open_config_file).pack(side=tk.RIGHT, padx=5)
        about_frame = ttk.LabelFrame(self.settings_tab, text="About", padding=10)
        about_frame.grid(row=4, column=0, sticky="ew", pady=5)
        ttk.Label(about_frame, text=f"{APP_NAME} {APP_VERSION_LABEL}").pack(anchor="w")
        ttk.Label(about_frame, text=APP_AUTHOR, foreground="gray").pack(anchor="w")
        ttk.Label(about_frame, text="A comprehensive tool for managing Minecraft Bedrock Dedicated Servers.", foreground="gray").pack(anchor="w", pady=(5, 0))
    
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
                foreground="#4CAF50")
        else:
            self.remote_status_label.config(text="○ Not accepting remote administration.", foreground="gray")

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
        if hasattr(self, 'console_text'):
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
                self._server_folder_state = "valid"
                version = detect_server_version(p_server_path)
                self.server_status.config(text=f"✅ {msg} | Version: {version}", foreground="green")
                self.update_server_info()
            elif p_server_path.exists() and p_server_path.is_dir() and not any(p_server_path.iterdir()):
                self._server_folder_state = "empty"
                self.server_status.config(text="❓ Folder is empty (Ready for Install)", foreground="blue")
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
                self.server_status.config(text=f"❌ {msg}", foreground="red")
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
        self.update_state_label.config(text=state, foreground=colour)
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
        if len(self.config.get("server_profiles", {})) < 2:
            self.update_all_button.pack_forget()
            return
        if not self.update_all_button.winfo_manager():
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
        row_index = 0
        self._absent_optional = {k for k in self.OPTIONAL_CHECKBOXES if k not in props}
        for key, (text, default, tip) in self.OPTIONAL_CHECKBOXES.items():
            var = tk.BooleanVar(value=props.get(key, default).strip().lower() == "true")
            ttk.Label(self.scrollable_frame, text=key).grid(row=row_index, column=0, sticky="e", padx=5, pady=2)
            box = ttk.Checkbutton(self.scrollable_frame, text=text, variable=var)
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
                ttk.Checkbutton(self.scrollable_frame, variable=var).grid(
                    row=row_index, column=1, sticky="w", padx=5, pady=2)
                self.entries[key] = _CheckValue(var)
                row_index += 1
                continue
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
            self.app.log("Server properties saved successfully.", "success")
            messagebox.showinfo("Success", "Properties saved!")
        else:
            messagebox.showerror("Error", "Failed to save properties.")


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

if __name__ == "__main__":
    main()
