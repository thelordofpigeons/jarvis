@echo off
rem Opens the JARVIS avatar (the hub's /face page) in an app-mode window: no tabs, no address bar.
rem Needs the hub running (jarvis hub). The port is [hub].port from jarvis.toml, read the way the CLI reads it
rem (jarvis.local.toml merged); if Python or the config cannot be read it falls back to 8765, the default.
rem Pass a port to override: bin\face-window.cmd 9000
setlocal
set "JARVIS_HOME=%~dp0..\"
set "JARVIS_PY=%JARVIS_HOME%.venv\Scripts\python.exe"
if not exist "%JARVIS_PY%" set "JARVIS_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set "PORT=%~1"
if not defined PORT (
  pushd "%JARVIS_HOME%"
  for /f "usebackq delims=" %%P in (`"%JARVIS_PY%" -c "from jarvisd.config import load_config; print(load_config().hub.port)" 2^>nul`) do set "PORT=%%P"
  popd
)
if not defined PORT set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/face"
set "SIZE=--window-size=420,460"

rem msedge ships with Windows 11; chrome is the fallback. "start" resolves both through App Paths.
start "" msedge --app=%URL% %SIZE% 2>nul && exit /b 0
start "" chrome --app=%URL% %SIZE% 2>nul && exit /b 0
echo face-window: neither msedge nor chrome could be started. Open %URL% in a browser.
exit /b 1
