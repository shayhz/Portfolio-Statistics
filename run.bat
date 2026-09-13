@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :fail
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

if not exist ".env" (
    echo.
    echo No .env file found. Copy .env.example to .env and add your
    echo tastytrade OAuth client ID, secret and refresh token, then run
    echo this again.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" app.py %*
if errorlevel 1 pause
exit /b 0

:fail
echo.
echo Setup failed. Check that Python 3.11+ is installed and on your PATH.
pause
exit /b 1
