@echo off
setlocal
set "app=%~dp0dist\LuminaPendantPrep\LuminaPendantPrep.exe"
if not exist "%app%" (
  echo The installed application was not found. Run scripts\build_windows.ps1 first.
  pause
  exit /b 1
)
start "" "%app%"
