@echo off
rem Opens the JARVIS avatar (the hub's /face page) in an app-mode window: no tabs, no address bar.
rem Needs the hub running (jarvis hub). The port is [hub].port from jarvis.toml, read the way the CLI reads it
rem (jarvis.local.toml merged); if Python or the config cannot be read it falls back to 8765, the default.
rem Pass a port to override: bin\face-window.cmd 9000
setlocal EnableDelayedExpansion
set "JARVIS_HOME=%~dp0..\"
set "JARVIS_PY=%JARVIS_HOME%.venv\Scripts\python.exe"
if not exist "%JARVIS_PY%" set "JARVIS_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "PORT=%~1"
if not defined PORT (
  rem bin\hub-port.py exits with the port (1024..65535), or 0 when the config cannot be read. An exit code
  rem needs no quoting; a `for /f` over `python -c "..."` breaks on the parentheses in the Python.
  "%JARVIS_PY%" "%~dp0hub-port.py" >nul 2>&1
  if errorlevel 1024 set "PORT=!ERRORLEVEL!"
)
if not defined PORT set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/face"
set "SIZE=--window-size=420,460"

rem msedge ships with Windows 11; chrome is the fallback. "start" resolves both through App Paths.
start "" msedge --app=%URL% %SIZE% 2>nul && exit /b 0
start "" chrome --app=%URL% %SIZE% 2>nul && exit /b 0
echo face-window: neither msedge nor chrome could be started. Open %URL% in a browser.
exit /b 1
