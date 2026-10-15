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

rem First-run (idempotent): ensure .env exists, print dashboard URL to stdout
set "OPEN_URL="
for /f "usebackq delims=" %%u in (`"%PY%" portable\first_run_setup.py`) do set "OPEN_URL=%%u"
if "%OPEN_URL%"=="" set "OPEN_URL=http://127.0.0.1:18080/dashboard/"

rem Port for readiness check (matches .env; default 18080)
set "PORT=18080"
if exist ".env" for /f "tokens=2 delims==" %%v in ('findstr /b /i "AIMC_PORT=" .env') do set "PORT=%%v"

echo.
echo ==============================================
echo   AIMC starting on port %PORT% ...
echo   Waiting for server readiness (max 120s)
echo ==============================================
echo.

rem Run server in background of this console; closing window stops it
start "AIMC-server" /b "%PY%" main.py

rem Poll /health until ready, then open the browser
set /a TRIES=0
:waitloop
curl -s -m 2 "http://127.0.0.1:%PORT%/health" >nul 2>&1
if %errorlevel%==0 goto ready
set /a TRIES+=1
if %TRIES% geq 60 (
    echo.
    echo [WARN] Server not ready after 120s. Check the logs above.
    echo        You can open %OPEN_URL% manually later.
    pause
    exit /b 1
)
rem ~2s wait (ping trick: robust in all console contexts)
ping -n 3 127.0.0.1 >nul
goto waitloop

:ready
echo Ready after ~%TRIES%x2 s. Opening dashboard: %OPEN_URL%
echo Keep this window open. Close it (or run stop-aimc.bat) to stop.
echo.
start "" "%OPEN_URL%"
pause
