@echo off
setlocal
cd /d "%~dp0"

rem The project folder is synced between computers, and a venv only works on the
rem machine that built it (it records that machine's Python path). So each
rem machine keeps its own venv outside the synced folder.
set "VENV=%LOCALAPPDATA%\PortfolioStatistics\venv"
set "PY=%VENV%\Scripts\python.exe"

rem A venv whose base Python was moved or uninstalled still has python.exe but
rem cannot start, so test that it runs rather than that it exists.
if exist "%PY%" "%PY%" -c "" >nul 2>&1 || goto :create
if not exist "%PY%" goto :create
goto :deps

:create
echo Creating virtual environment in %VENV% ...
if exist "%VENV%" rmdir /s /q "%VENV%"
rem Prefer the py launcher: a bare "python" may be the Microsoft Store stub.
where py >nul 2>&1 && (py -3 -m venv "%VENV%" || goto :fail) || (python -m venv "%VENV%" || goto :fail)
"%PY%" -m pip install --upgrade pip

:deps
rem Reinstall whenever requirements.txt has changed since the last install.
fc /b requirements.txt "%VENV%\requirements.txt" >nul 2>&1 && goto :run
echo Installing dependencies...
"%PY%" -m pip install -r requirements.txt || goto :fail
copy /y requirements.txt "%VENV%\requirements.txt" >nul

:run
if not exist ".env" (
    echo.
    echo No .env file found. Copy .env.example to .env and add your
    echo tastytrade OAuth client ID, secret and refresh token, then run
    echo this again.
    echo.
    pause
    exit /b 1
)

"%PY%" app.py %*
if errorlevel 1 pause
exit /b 0

:fail
echo.
echo Setup failed. Check that Python 3.11+ is installed and on your PATH.
pause
exit /b 1
