# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Bedrock Server Manager (PyInstaller 6.x).
# Build from the repo root:  pyinstaller bedrock_updater.spec --noconfirm
# Produces a one-folder bundle in dist/BedrockServerManager/ on whichever OS it runs.
import sys

APP_NAME = "BedrockServerManager"

a = Analysis(
    ['bedrock_updater_linux.py'],
    pathex=[],
    binaries=[],
    # Bundle the icon so set_window_icon() finds it via sys._MEIPASS at runtime.
    datas=[('minecraft.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Embedded exe icon is Windows-only; Linux/macOS ignore this.
    icon='minecraft.ico' if sys.platform == 'win32' else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
