@echo off
rem Opens the JARVIS hub in an app-mode window (no tabs, no address bar) and starts the hub first when
rem nothing listens on its port. This is what the "JARVIS Hub" shortcut runs (deploy\make-shortcuts.ps1).
rem
rem Port: [hub].port read the way the CLI reads it (jarvis.local.toml merged); if Python or the config
rem cannot be read it falls back to 8765, the default. Pass a port to override: bin\hub.cmd 9000
rem Start order when the port is silent: the JarvisHub scheduled task (deploy\register-hub-task.ps1)
rem if it is registered, else pythonw -m jarvisd hub detached from this window.
setlocal EnableDelayedExpansion
set "JARVIS_HOME=%~dp0..\"
set "JARVIS_PY=%JARVIS_HOME%.venv\Scripts\python.exe"
set "JARVIS_PYW=%JARVIS_HOME%.venv\Scripts\pythonw.exe"
if not exist "%JARVIS_PY%" set "JARVIS_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%JARVIS_PYW%" set "JARVIS_PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
set "PORT=%~1"
if not defined PORT (
  rem bin\hub-port.py exits with the port (1024..65535), or 0 when the config cannot be read. An exit code
  rem needs no quoting; a `for /f` over `python -c "..."` breaks on the parentheses in the Python.
  "%JARVIS_PY%" "%~dp0hub-port.py" >nul 2>&1
  if errorlevel 1024 set "PORT=!ERRORLEVEL!"
)
if not defined PORT set "PORT=8765"
set "URL=http://127.0.0.1:%PORT%/"
set "SIZE=--window-size=1180,820"

call :listening
if not errorlevel 1 goto open

schtasks /query /tn JarvisHub >nul 2>&1
if not errorlevel 1 (
  schtasks /run /tn JarvisHub >nul 2>&1
) else (
  pushd "%JARVIS_HOME%"
  start "" "%JARVIS_PYW%" -m jarvisd hub
  popd
)

set /a TRIES=0
:wait
call :listening
if not errorlevel 1 goto open
set /a TRIES+=1
if %TRIES% geq 20 goto failed
timeout /t 1 /nobreak >nul
goto wait

:open
rem msedge ships with Windows 11; chrome is the fallback. "start" resolves both through App Paths.
start "" msedge --app=%URL% %SIZE% 2>nul && exit /b 0
start "" chrome --app=%URL% %SIZE% 2>nul && exit /b 0
echo hub: neither msedge nor chrome could be started. Open %URL% in a browser.
exit /b 1

:failed
echo hub: nothing answered on 127.0.0.1:%PORT% after 20 seconds. Run "jarvis hub" in a terminal to see why.
exit /b 1

:listening
netstat -ano -p tcp | findstr /c:"127.0.0.1:%PORT% " | findstr /c:"LISTENING" >nul
exit /b %errorlevel%
