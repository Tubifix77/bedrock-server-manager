# Packaging & releases

The app is a single cross-platform script ([`../bedrock_updater_linux.py`](../bedrock_updater_linux.py)).
These files turn it into standalone, install-anywhere artifacts that need **no Python installed** on
the user's machine:

| Platform | Tool | Output |
|----------|------|--------|
| Windows  | PyInstaller → **Inno Setup** | `BedrockServerManager-<ver>-Setup.exe` (folder-picker installer + shortcuts) |
| Linux    | PyInstaller → **AppImage**   | `BedrockServerManager-<ver>-x86_64.AppImage` (run-anywhere) |

Both bundle Python + Tcl/Tk via **PyInstaller** ([`../bedrock_updater.spec`](../bedrock_updater.spec)),
which also bundles `minecraft.png` so the in-app window icon works in a frozen build
(`set_window_icon()` finds it via `sys._MEIPASS`).

## Release (recommended): tag and let CI build both

[`.github/workflows/release.yml`](../.github/workflows/release.yml) builds the Windows installer
and the Linux AppImage and attaches them to a GitHub Release:

```bash
git tag v2.0.0 && git push origin v2.0.0
```

The tag name (minus the leading `v`) becomes the artifact version. You can also run the workflow
manually from the Actions tab (**Run workflow**) to build artifacts without publishing a release.

## Building locally

**Windows** (needs [Inno Setup 6](https://jrsoftware.org/isdl.php) on PATH or at its default location):

```powershell
py -m venv .buildvenv; .buildvenv\Scripts\pip install pyinstaller
.buildvenv\Scripts\pyinstaller bedrock_updater.spec --noconfirm --clean
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\windows\installer.iss
# -> dist_installer\BedrockServerManager-2.0.0-Setup.exe
```

**Linux** (needs `python3-tk` and `curl`):

```bash
python3 -m pip install pyinstaller
packaging/linux/build-appimage.sh 2.0.0
# -> BedrockServerManager-2.0.0-x86_64.AppImage
```

## Regenerating the Windows icon

`minecraft.ico` (multi-size, committed) is generated from `minecraft.png` with Pillow:

```python
from PIL import Image
Image.open("minecraft.png").convert("RGBA").save(
    "minecraft.ico", format="ICO", sizes=[(16,16),(32,32),(48,48),(64,64),(128,128)])
```

## When bumping the version

Keep these in sync: `APP_VERSION` in [`../bedrock_updater_linux.py`](../bedrock_updater_linux.py),
the README badge, and the git tag (the tag drives the artifact filenames; the Inno script and
AppImage script default to `2.0.0` only when built outside CI).

## Notes

- **Unsigned.** Artifacts are not code-signed, so Windows SmartScreen shows a
  "Windows protected your PC / unknown publisher" prompt (**More info → Run anyway**), and the
  AppImage just needs `chmod +x`. Code signing requires a paid certificate.
- The Linux AppImage is built on Ubuntu 22.04 for broad glibc compatibility (runs on Debian 12 etc.).
