@echo off
rem AIMC Portable launcher - ASCII only (cmd reads .bat in OEM codepage)
setlocal
cd /d "%~dp0"
set "PY=%~dp0python\python.exe"

if not exist "%PY%" (
    echo [ERROR] python runtime not found: %PY%
    echo Package may be incomplete - please re-download.
    pause
    exit /b 1
)

set "OPEN_URL="
for /f "usebackq delims=" %%u in (`"%PY%" portable\first_run_setup.py`) do set "OPEN_URL=%%u"
if "%OPEN_URL%"=="" set "OPEN_URL=http://127.0.0.1:18080/dashboard/"

echo.
echo ==============================================
echo   AIMC starting...
echo   Dashboard: %OPEN_URL%
echo   Keep this window open. Close it (or Ctrl+C) to stop.
echo ==============================================
echo.
start "" "%OPEN_URL%"
"%PY%" main.py
echo.
echo AIMC exited.
pause
