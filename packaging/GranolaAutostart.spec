# PyInstaller spec for the one-folder build. Run it through build.ps1, which draws build/app.ico first.
#
# One folder, not one file: a one-file exe unpacks itself to a temp folder on every launch, which
# delays each countdown card and is the usual cause of antivirus false positives.

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
BUILD = ROOT / "build"
BUILD.mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from granola_autostart import __version__  # noqa: E402


def version_resource() -> str:
    """The Windows file properties (Details tab) for GranolaAutostart.exe."""
    numbers = tuple(int(part) for part in __version__.split(".")) + (0, 0, 0)
    numbers = numbers[:4]
    path = BUILD / "version_info.txt"
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Conrad Foucher'),
      StringStruct('FileDescription', 'Granola Autostart'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'GranolaAutostart'),
      StringStruct('OriginalFilename', 'GranolaAutostart.exe'),
      StringStruct('ProductName', 'Granola Autostart'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )
    return str(path)


from PyInstaller.utils.hooks import collect_submodules  # noqa: E402

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    # The Settings and setup page, read from _internal/granola_autostart/web at runtime, beside settings_app.
    datas=[(str(ROOT / "granola_autostart" / "web"), "granola_autostart/web")],
    # Imports chosen at runtime, which the analysis cannot see: pystray's Windows backend, pywebview's
    # Edge WebView2 platform, and the WinRT bindings behind Windows notifications.
    hiddenimports=[
        "pystray._win32",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        *collect_submodules("windows_toasts"),
        *collect_submodules("winrt"),
    ],
    excludes=["pytest", "_pytest", "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GranolaAutostart",
    console=False,
    upx=False,
    icon=str(BUILD / "app.ico"),
    version=version_resource(),
)
coll = COLLECT(exe, a.binaries, a.datas, name="GranolaAutostart", upx=False)
