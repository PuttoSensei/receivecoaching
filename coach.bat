@echo off
REM Double-click or run from any terminal to open the coach.
REM Any args after `coach` get passed straight through, so you can use:
REM   coach                          (default / last-used coach)
REM   coach --coach grief            (specific coach)
REM   coach --list-coaches           (print all 43)
REM   coach --coach-info procrastination

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [error] Python was not found on PATH. Install Python 3 from https://python.org and re-run.
    pause
    exit /b 1
)

python receive_coach.py %*
