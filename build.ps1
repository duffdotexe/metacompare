# Build MetaCompare.exe into .\dist\ using PyInstaller.
# Usage:  .\build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    py -3 -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt --quiet

& .\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm --clean --onefile --windowed `
    --name MetaCompare `
    --icon assets\icon.ico `
    --collect-all tkinterdnd2 `
    --collect-submodules hachoir `
    --collect-all pillow_heif `
    main.py

Write-Host "`nDone: $(Resolve-Path .\dist\MetaCompare.exe)"
