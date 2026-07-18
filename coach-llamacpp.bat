@echo off
REM ============================================================================
REM  One-click launcher: starts llama-server (if not already running), then
REM  opens the coach pointed at it. Double-click this file.
REM
REM  If llama-server is already running on port 8090, reuses it (no wait).
REM  Close the coach terminal when done; the server keeps running for next time.
REM  To stop the server, close its window or run stop-llama-server.bat.
REM
REM  Args pass through to the coach:
REM     coach-llamacpp                        (default / last coach)
REM     coach-llamacpp --coach grief          (specific coach)
REM     coach-llamacpp --list-coaches         (list all 43 and exit)
REM ============================================================================

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python was not found on PATH. Install Python 3 from https://python.org and re-run.
    pause
    exit /b 1
)

REM Check if llama-server is already up
curl -s -m 2 http://127.0.0.1:8090/health >nul 2>&1
if %errorlevel% equ 0 (
    echo [ok] llama-server already running on port 8090, reusing it.
    goto run_coach
)

echo [info] Starting llama-server in a separate window...
start "llama-server" /min cmd /c start-llama-server.bat

echo [info] Waiting for model to load ^(this can take 10-30s the first time^)...
set /a tries=0
:wait_loop
timeout /t 2 /nobreak >nul
curl -s -m 2 http://127.0.0.1:8090/health >nul 2>&1
if %errorlevel% equ 0 goto server_ready
set /a tries+=1
if %tries% geq 30 (
    echo.
    echo [error] llama-server didn't respond after 60s.
    echo         Check the llama-server window for errors. Common issues:
    echo           - MODEL path in start-llama-server.bat doesn't exist
    echo           - Not enough RAM for the GGUF
    echo           - Port 8090 already in use by something else
    echo.
    pause
    exit /b 1
)
goto wait_loop

:server_ready
echo [ok] Server ready.

:run_coach
set RECEIVE_COACH_BASE_URL=http://127.0.0.1:8090/v1
set RECEIVE_COACH_EMBED_FORMAT=openai

python receive_coach.py %*
