<#
.SYNOPSIS
    Build every Ensemble-AI-Studio release artifact with one command (packaging plan 2.3).

.DESCRIPTION
    Produces, from a single run:

      dist/release/Ensemble-AI-Studio-<ver>-Setup.exe      Inno Setup installer (one-folder payload)
      dist/release/Ensemble-AI-Studio-<ver>-portable.zip   one-folder build, unzip and run
      dist/release/Ensemble-AI-Studio-<ver>-onefile.exe    single-file build (slower first start)
      dist/release/ensemble-<ver>-py3-none-any.whl         Python wheel (for `pip install ensemble`)
                                                          CODE ONLY: no Qt, no materials

    The version comes from app/pyproject.toml only - never from this script, never
    duplicated into the spec or the .iss (the spec reads it, this script forwards it).

    NOTE ON ENCODING: this file is deliberately ASCII-only. Windows PowerShell 5.1
    decodes .ps1 files as ANSI unless they carry a UTF-8 BOM, so a UTF-8 file with
    non-ASCII comments is read back as mojibake, and mojibake is a parse risk. All
    Chinese explanation lives in packaging/README.md instead.

.PARAMETER Python
    Python interpreter to build with. Defaults to <repo>/app/.venv/Scripts/python.exe,
    then to `python` on PATH.

.PARAMETER Baseline
    Also build a non-slimmed one-folder tree, for the before/after size measurement
    (packaging plan 2.1). It is written to dist/ but never copied to dist/release/.

.PARAMETER SourceRoot
    Optional overlay: use this tree's app/src as the source code (its app/ materials
    are still taken from the repo this script lives in). Needed only while a
    downstream repo lags the source tree; the released repo builds with the default.

.PARAMETER SkipTests / SkipVerify / SkipInstaller / SkipWheel
    Skip the corresponding step.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$SourceRoot = "",
    [switch]$Baseline,
    [switch]$SkipTests,
    [switch]$SkipVerify,
    [switch]$SkipInstaller,
    [switch]$SkipWheel
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AppDir = Join-Path $RepoRoot "app"
$DistDir = Join-Path $RepoRoot "dist"
$WorkDir = Join-Path $RepoRoot "build"
$ReleaseDir = Join-Path $DistDir "release"
$Spec = Join-Path $PSScriptRoot "ensemble.spec"
$Iss = Join-Path $PSScriptRoot "installer.iss"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ("=" * 72) -ForegroundColor DarkCyan
}

function Assert-Ok([string]$What) {
    if ($LASTEXITCODE -ne 0) { throw "$What failed with exit code $LASTEXITCODE" }
}

function Get-ProjectVersion {
    # The one and only source of the version number: [project].version in pyproject.toml.
    $text = Get-Content -Raw -Encoding UTF8 (Join-Path $AppDir "pyproject.toml")
    $m = [regex]::Match($text, '(?m)^\s*version\s*=\s*"([^"]+)"')
    if (-not $m.Success) { throw "No [project].version found in app/pyproject.toml" }
    return $m.Groups[1].Value
}

function Find-Python {
    # Release builds must use a known, self-contained environment (packaging plan,
    # "dependency drift" risk). Never quietly fall back to whatever `python` happens
    # to be first on PATH: on a machine with a user-site PyInstaller that silently
    # builds a broken artifact with the wrong dependency set.
    if ($Python) {
        if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
        return (Resolve-Path $Python).Path
    }
    $venv = Join-Path $AppDir ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    # No venv yet: create one exactly as the Quickstart (and packaging plan 2.3 step 1)
    # describes. This is the only step that needs the network.
    Write-Host "  no venv at $venv - creating it (pip install needs network)" -ForegroundColor Yellow
    $base = Get-Command python -ErrorAction SilentlyContinue
    if (-not $base) { throw "No Python interpreter found. Pass -Python <path>." }
    & $base.Source -m venv (Join-Path $AppDir ".venv")
    Assert-Ok "python -m venv"
    $venvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -e "$AppDir[dev,gui]" pyinstaller
    Assert-Ok "pip install -e '.[dev,gui]' pyinstaller"
    return $venvPython
}

function Assert-Provisioned([string]$Py) {
    # The interpreter must actually have everything the bundle needs - most of all
    # PySide6, whose absence produces a confusing downstream failure (a FileNotFound
    # on base_library.zip) instead of a clear one.
    $code = "import PyInstaller, PySide6, langgraph, langgraph.checkpoint.sqlite, " +
            "pydantic, httpx, yaml, aiosqlite; print('ok')"
    # Windows PowerShell 5.1 turns redirected *stderr* from a native command into a
    # terminating error under $ErrorActionPreference = "Stop" - which would abort the
    # build with PowerShell's own message instead of the probe output below.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $probe = & $Py -c $code 2>&1
    $probeCode = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($probeCode -ne 0) {
        throw ("The interpreter is not provisioned for a release build:`n  $Py`n" +
               "Missing a dependency of PyInstaller/PySide6/harness:`n$probe`n" +
               "Create the venv first (python -m venv app\.venv; " +
               "app\.venv\Scripts\pip install -e `".\app[dev,gui]`" pyinstaller) " +
               "or pass -Python <path-to-a-provisioned-interpreter>.")
    }
}

function Find-Iscc {
    if ($env:INNO_SETUP_ISCC -and (Test-Path $env:INNO_SETUP_ISCC)) { return $env:INNO_SETUP_ISCC }
    foreach ($candidate in @(
            "D:\Inno Setup 6\iscc.exe",
            "C:\Program Files (x86)\Inno Setup 6\iscc.exe",
            "C:\Program Files\Inno Setup 6\iscc.exe")) {
        if (Test-Path $candidate) { return $candidate }
    }
    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ""
}

function Invoke-PyInstaller([string]$Form, [bool]$Slim, [string]$OutName, [string]$Dist = "") {
    # The form and the trimming are spec inputs, not CLI flags: once a .spec is
    # passed, --onedir/--onefile are silently ignored by PyInstaller.
    if (-not $Dist) { $Dist = $DistDir }
    $env:ENSEMBLE_FORM = $Form
    $env:ENSEMBLE_EXCLUDE_QT = $(if ($Slim) { "1" } else { "0" })
    if ($SourceRoot) { $env:ENSEMBLE_SRC = (Join-Path $SourceRoot "src") }
    $started = Get-Date
    & $Python -m PyInstaller --noconfirm --clean --log-level WARN `
        --distpath $Dist --workpath (Join-Path $WorkDir ("work-" + $OutName + "-" + $Form)) `
        $Spec
    Assert-Ok "PyInstaller ($Form, slim=$Slim)"
    $elapsed = (Get-Date) - $started
    $target = Join-Path $Dist $OutName
    $bytes = 0
    if (Test-Path $target -PathType Container) {
        $bytes = (Get-ChildItem $target -Recurse -File | Measure-Object -Property Length -Sum).Sum
    } elseif (Test-Path $target) {
        $bytes = (Get-Item $target).Length
    }
    Write-Host ("  built {0}  {1:N1} MB  in {2:mm\:ss}" -f $OutName, ($bytes / 1MB), $elapsed) -ForegroundColor Green
    return $bytes
}

function Invoke-Selftest([string]$Exe, [string]$Tag) {
    # Headless proof that the artifact starts, resolves its materials and can build
    # a QApplication: the packaged app has no console, so the entry script writes a
    # report file instead (see packaging/entry_gui.py).
    $report = Join-Path $WorkDir ("selftest-" + $Tag + ".txt")
    if (Test-Path $report) { Remove-Item $report -Force }
    $env:QT_QPA_PLATFORM = "offscreen"
    $env:ENSEMBLE_SELFTEST = $report
    try {
        & $Exe | Out-Null
    } catch {
        # A non-zero exit is the expected path when the report says FAIL; keep going
        # so the report itself is what gets inspected.
    }
    Remove-Item Env:\ENSEMBLE_SELFTEST -ErrorAction SilentlyContinue
    if (-not (Test-Path $report)) { throw "Selftest produced no report: $report" }
    $text = Get-Content -Raw -Encoding UTF8 $report
    Write-Host $text
    if ($text -notmatch "RESULT: OK") { throw "Selftest FAILED for $Tag (see $report)" }
    Write-Host "  selftest OK: $Tag" -ForegroundColor Green
}

# ------------------------------------------------------------------ 1. prepare
Write-Step "1/7  Resolve toolchain"
$Version = Get-ProjectVersion
$Python = Find-Python
$ProductBase = "Ensemble-AI-Studio-$Version"
Write-Host "  repo      : $RepoRoot"
Write-Host "  python    : $Python"
Write-Host "  version   : $Version"
Assert-Provisioned $Python
$pyver = & $Python -c "import sys, PyInstaller, PySide6; print(sys.version.split()[0], PyInstaller.__version__, PySide6.__version__)"
Write-Host "  py/pi/qt  : $pyver"
if ($SourceRoot) { Write-Host "  source overlay: $SourceRoot" -ForegroundColor Yellow }

New-Item -ItemType Directory -Force -Path $DistDir, $WorkDir, $ReleaseDir | Out-Null

# ------------------------------------------------------------------ 2. tests
if (-not $SkipTests) {
    Write-Step "2/7  Run the test suite (must be green before anything ships)"
    Push-Location $AppDir
    try {
        & $Python -m pytest -q
        Assert-Ok "pytest"
    } finally { Pop-Location }
} else {
    Write-Step "2/7  Tests skipped (-SkipTests)"
}

# ------------------------------------------------------------------ 3. baseline
if ($Baseline) {
    Write-Step "3/7  Baseline one-folder build (no Qt trimming) - size comparison only"
    # A separate --distpath: the spec names both variants <name>-<ver>-<form>, so a
    # shared dist/ would let the slim build silently overwrite the baseline one.
    $BaselineDist = Join-Path $RepoRoot "dist-baseline"
    Invoke-PyInstaller -Form "onedir" -Slim $false -OutName "$ProductBase-onedir" -Dist $BaselineDist | Out-Null
} else {
    Write-Step "3/7  Baseline build skipped (pass -Baseline for the before/after sizes)"
}

# ------------------------------------------------------------------ 4. onedir + onefile
Write-Step "4/7  One-folder build (installer payload)"
Invoke-PyInstaller -Form "onedir" -Slim $true -OutName "$ProductBase-onedir" | Out-Null

Write-Step "5/7  One-file build (portable single exe)"
Invoke-PyInstaller -Form "onefile" -Slim $true -OutName "$ProductBase-onefile.exe" | Out-Null

# ------------------------------------------------------------------ 6. verify
if (-not $SkipVerify) {
    Write-Step "6/7  Verify the artifacts start headless"
    Invoke-Selftest (Join-Path $DistDir "$ProductBase-onedir\Ensemble-AI-Studio.exe") "onedir"
    Invoke-Selftest (Join-Path $DistDir "$ProductBase-onefile.exe") "onefile"
} else {
    Write-Step "6/7  Verify skipped (-SkipVerify)"
}

# ------------------------------------------------------------------ 7. package
Write-Step "7/7  Package: portable zip, installer, wheel"

$OneDir = Join-Path $DistDir "$ProductBase-onedir"
$PortableZip = Join-Path $ReleaseDir "$ProductBase-portable.zip"
if (Test-Path $PortableZip) { Remove-Item $PortableZip -Force }
Compress-Archive -Path (Join-Path $OneDir "*") -DestinationPath $PortableZip -CompressionLevel Optimal
Write-Host ("  {0}  {1:N1} MB" -f (Split-Path $PortableZip -Leaf), ((Get-Item $PortableZip).Length / 1MB)) -ForegroundColor Green

Copy-Item (Join-Path $DistDir "$ProductBase-onefile.exe") $ReleaseDir -Force

if (-not $SkipInstaller) {
    $iscc = Find-Iscc
    if (-not $iscc) {
        Write-Warning "iscc.exe not found (set INNO_SETUP_ISCC or install Inno Setup 6) - skipping Setup.exe"
    } else {
        & $iscc "/DMyAppVersion=$Version" $Iss
        Assert-Ok "iscc"
        $setup = Join-Path $ReleaseDir "$ProductBase-Setup.exe"
        Write-Host ("  {0}  {1:N1} MB" -f (Split-Path $setup -Leaf), ((Get-Item $setup).Length / 1MB)) -ForegroundColor Green
    }
} else {
    Write-Host "  installer skipped (-SkipInstaller)"
}

if (-not $SkipWheel) {
    Push-Location $AppDir
    try {
        # CODE ONLY: the wheel carries harness/**.py + dist-info and nothing else.
        # The materials (characters/scenes/config/templates) live in app/ and the
        # repo root, not inside the import package, and a wheel can only install
        # packages into site-packages. So a pip-only user gets the engine, the
        # commands and the desktop-app code - not the demo scene. The README's
        # Install table states this; packaging/README.md explains why bundling
        # them would mean touching resource_dir()/materials_dir() semantics too.
        # --no-isolation / --no-build-isolation: the build must work on a machine
        # with no (or no trusted) network, using the setuptools already installed.
        # ($ErrorActionPreference is loosened around the redirects - see Assert-Provisioned.)
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $Python -m build --wheel --no-isolation --outdir $ReleaseDir 2>&1 | Write-Host
        $wheelCode = $LASTEXITCODE
        if ($wheelCode -ne 0) {
            Write-Warning "'python -m build' unavailable - falling back to 'pip wheel'"
            & $Python -m pip wheel . --no-deps --no-build-isolation --wheel-dir $ReleaseDir 2>&1 | Write-Host
            $wheelCode = $LASTEXITCODE
        }
        $ErrorActionPreference = $prev
        if ($wheelCode -ne 0) { throw "wheel build failed with exit code $wheelCode" }
    } finally { Pop-Location }
} else {
    Write-Host "  wheel skipped (-SkipWheel)"
}

Write-Step "Done - artifacts in dist/release"
Get-ChildItem $ReleaseDir | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-46} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
