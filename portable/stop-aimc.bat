@echo off
rem AIMC Portable stopper - ASCII only
setlocal enabledelayedexpansion
set "PORT=18080"
if exist ".env" (
    for /f "tokens=2 delims==" %%v in ('findstr /b /i "AIMC_PORT=" .env') do set "PORT=%%v"
)
set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
    set FOUND=1
)
if "!FOUND!"=="1" (
    echo AIMC stopped.
) else (
    echo No process listening on port !PORT!.
)
pause
