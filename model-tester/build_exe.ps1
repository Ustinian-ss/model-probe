$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ScriptDir ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment not found: $VenvPython. Run: python -m venv .venv && .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

& $VenvPython -m pip install -r requirements.txt
# --add-data 必须带上 config/models.json，否则打包后的 exe 内置模型清单为空
& $VenvPython -m PyInstaller --noconfirm --onefile --windowed --name "NvidiaModelTester" --add-data "config;config" src/main.py

Write-Host "Built: dist\NvidiaModelTester.exe"
