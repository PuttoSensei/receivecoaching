@echo off
REM Start raw llama.cpp llama-server with a GGUF, configured for both chat and embeddings.
REM
REM Edit the MODEL path below to your .gguf file, then double-click this file (or run
REM from a terminal). Leave this terminal running while using the coach.
REM
REM In another terminal, run:
REM   set RECEIVE_COACH_BASE_URL=http://127.0.0.1:8090/v1
REM   set RECEIVE_COACH_EMBED_FORMAT=openai
REM   coach
REM
REM Important: --pooling mean is required for embeddings from chat models.
REM --embeddings enables the /v1/embeddings endpoint.

set MODEL=C:\Heya\novel-creator\Meta-Llama-3.1-8B-Instruct-Q3_K_M.gguf
set CTX=4096
set PORT=8090

where llama-server >nul 2>nul
if errorlevel 1 (
    echo [error] llama-server was not found on PATH. Install with: winget install ggml.llamacpp
    pause
    exit /b 1
)

if not exist "%MODEL%" (
    echo [error] Model file not found: %MODEL%
    echo         Edit the MODEL= line in this file to point at your .gguf.
    pause
    exit /b 1
)

llama-server -m "%MODEL%" -c %CTX% --port %PORT% --embeddings --pooling mean

pause
