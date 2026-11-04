@echo off
setlocal
set "root=%LOCALAPPDATA%\LuminaPendantPrep"
rem Codex Desktop can redirect LOCALAPPDATA writes into its package cache.
if not exist "%root%\current.txt" set "root=%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\LuminaPendantPrep"
if not exist "%root%\current.txt" (
  echo No installed version was found. Run scripts\build_windows.ps1 first.
  pause
  exit /b 1
)
set /p "version=" < "%root%\current.txt"
set "app=%root%\versions\%version%\LuminaPendantPrep.exe"
if not exist "%app%" (
  echo The installed application was not found. Run scripts\build_windows.ps1 first.
  pause
  exit /b 1
)
start "" "%app%"
