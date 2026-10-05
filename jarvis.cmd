@echo off
rem JARVIS CLI shim: jarvis <subcommand>. Runs the venv Python, falling back to the system
rem Python 3.12 until deploy\setup-venv.ps1 has been run. cwd is the repo root so that
rem "-m jarvisd" resolves without installing the package.
setlocal
set "JARVIS_HOME=%~dp0"
set "JARVIS_PY=%JARVIS_HOME%.venv\Scripts\python.exe"
if not exist "%JARVIS_PY%" set "JARVIS_PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
pushd "%JARVIS_HOME%"
"%JARVIS_PY%" -m jarvisd %*
set "JARVIS_RC=%ERRORLEVEL%"
popd
exit /b %JARVIS_RC%
