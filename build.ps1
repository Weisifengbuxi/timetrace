# Build TimeTrace.exe with PyInstaller.
#   powershell -ExecutionPolicy Bypass -File build.ps1
# Requires: Python 3.9+ and  pip install pyinstaller

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$common = @(
    "--onefile", "--noconfirm", "--clean",
    "--name", "TimeTrace",
    "--distpath", "dist", "--workpath", "build\tmp", "--specpath", "build",
    "--exclude-module", "tkinter",
    "--exclude-module", "unittest",
    "--exclude-module", "pydoc",
    "--exclude-module", "email",
    "--exclude-module", "http",
    "--exclude-module", "xml",
    "timetrace.py"
)

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m PyInstaller @common
} else {
    & python -m PyInstaller @common
}

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host ""
Write-Host "Built: dist\TimeTrace.exe" -ForegroundColor Green
