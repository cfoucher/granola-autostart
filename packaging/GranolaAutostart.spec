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
from app_version import __version__  # noqa: E402


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


a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    # pystray picks its Windows backend at runtime, so the analysis cannot see the import.
    hiddenimports=["pystray._win32"],
    excludes=["pytest", "_pytest"],
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
