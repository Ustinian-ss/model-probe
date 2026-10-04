@echo off
rem ============================================================
rem  Double-click me: always run the NEWEST code.
rem  If anything under src\ or config\ is newer than the exe,
rem  it rebuilds first (1-3 min), then launches.
rem
rem  Extra args are forwarded, e.g.:  this.cmd -Check
rem ============================================================
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0rebuild-if-stale.ps1" -Launch %*
if errorlevel 1 pause
endlocal
