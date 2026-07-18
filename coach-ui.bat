@echo off
REM ============================================================================
REM  Launch the desktop UI. One click:
REM    - Spawns the Python backend (server.py) on a free port
REM    - Opens the Electron window
REM    - Kills the backend when you close the window
REM
REM  First run only: npm install (if node_modules is missing).
REM
REM  Args: pass --dev to also open Chromium DevTools.
REM ============================================================================

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python was not found on PATH. The backend needs Python 3 with fastapi + uvicorn.
    echo         Install Python from https://python.org, then: pip install fastapi uvicorn
    pause
    exit /b 1
)

where npm >nul 2>nul
if errorlevel 1 (
    echo [error] Node.js / npm was not found on PATH. Install from https://nodejs.org and re-run.
    pause
    exit /b 1
)

if not exist node_modules (
    echo [info] First run: installing Electron dependencies...
    call npm install
    if errorlevel 1 (
        echo.
        echo [error] npm install failed. Check Node.js is installed ^(node --version^).
        pause
        exit /b 1
    )
)

npx electron . %*
