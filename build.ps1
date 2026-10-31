<#
Build the installer: tests, then the PyInstaller one-folder app, then the Inno Setup installer.

Needs requirements-build.txt installed (in .venv, or on PATH as in CI) and Inno Setup 7 (6 also works).
    .\build.ps1              # everything
    .\build.ps1 -SkipTests   # CI, where a separate job already ran the tests
The installer lands in dist\GranolaAutostart-<version>-setup.exe.
#>
param([switch]$SkipTests)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = if (Test-Path ".venv\Scripts\python.exe") { (Resolve-Path ".venv\Scripts\python.exe").Path } else { "python" }

function Invoke-Step([string]$Name, [scriptblock]$Command) {
    Write-Host "== $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE" }
}

function Find-Iscc {
    # Install folders first: a Scoop shim on PATH can point at a stale user folder.
    $scoop = if ($env:SCOOP) { $env:SCOOP } else { "$env:USERPROFILE\scoop" }
    $candidates = foreach ($major in 7, 6) {
        "$env:ProgramFiles\Inno Setup $major\ISCC.exe"
        "${env:ProgramFiles(x86)}\Inno Setup $major\ISCC.exe"
        "$env:LOCALAPPDATA\Programs\Inno Setup $major\ISCC.exe"
    }
    $candidates += "$scoop\apps\inno-setup\current\ISCC.exe"
    foreach ($candidate in $candidates) { if (Test-Path $candidate) { return $candidate } }
    $onPath = Get-Command iscc -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw "Inno Setup (ISCC.exe) was not found. Install it from jrsoftware.org or with 'choco install innosetup'."
}

if (-not $SkipTests) { Invoke-Step "tests" { & $python -m pytest -q } }

$version = (& $python -c "from granola_autostart import __version__; print(__version__)")
if ($LASTEXITCODE -ne 0) { throw "could not read the version from granola_autostart" }
$version = $version.Trim()

Invoke-Step "icon" { & $python packaging\make_icon.py build\app.ico }
Invoke-Step "app ($version)" {
    & $python -m PyInstaller packaging\GranolaAutostart.spec --noconfirm --clean --distpath dist --workpath build\pyinstaller
}
$iscc = Find-Iscc
Invoke-Step "installer" { & $iscc /Qp "/DAppVersion=$version" packaging\installer.iss }

Write-Host "Built dist\GranolaAutostart-$version-setup.exe"
