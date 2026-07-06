@echo off
cd /d "%~dp0"
title TossAutoTrade

echo ============================================
echo   TossAutoTrade - auto setup and run
echo ============================================

python --version >nul 2>&1
if errorlevel 1 goto NOPYTHON

if not exist ".venv" (
    echo [1/4] Creating virtual environment...
    python -m venv .venv
)

echo [2/4] Installing packages...
call .venv\Scripts\activate.bat
pip install -r requirements.txt -q

if not exist ".env" (
    echo [3/4] Creating .env file. Enter your CLIENT_ID / SECRET in notepad, then save and close.
    copy .env.example .env >nul
    notepad .env
)

echo [3/4] Stopping old server on port 8000 (if any)...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "0.0.0.0:8000.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "127.0.0.1:8000.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo [4/4] Starting server... browser will open.
start "" http://localhost:8000
python main.py
pause
exit /b 0

:NOPYTHON
echo [ERROR] Python is not installed.
echo Install from https://www.python.org/downloads/
echo IMPORTANT: check "Add Python to PATH" during install.
pause
exit /b 1
