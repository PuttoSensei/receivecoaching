@echo off
REM Stop the llama-server that start-llama-server.bat launched.
REM Kills only the process listening on our port (8090) — other llama-server
REM instances on the machine (other projects) are left alone.

set PORT=8090
set FOUND=0

echo Stopping llama-server on port %PORT%...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr LISTENING') do (
    taskkill /PID %%a /F >nul 2>nul
    set FOUND=1
)

if "%FOUND%"=="1" (
    echo [ok] llama-server on port %PORT% stopped.
) else (
    echo [info] Nothing listening on port %PORT%.
)
timeout /t 2 /nobreak >nul
