$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment not found: $VenvPython. Run: python -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

& $VenvPython -m pip install -r requirements.txt
& $VenvPython -m PyInstaller --noconfirm --onefile --windowed --name "NvidiaModelTester" src/main.py

Write-Host "Built: dist\NvidiaModelTester.exe"
