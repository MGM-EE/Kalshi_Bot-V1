@echo off
setlocal
cd /d "%~dp0"
title Kalshi Paper Bot
if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python 3.12 environment...
    py -3.12 -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import kalshi_bot.desktop, tzdata, fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo Installing the bot. This can take a few minutes on first launch...
    ".venv\Scripts\python.exe" -m pip install -e .
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m kalshi_bot.desktop %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo The bot could not start. Copy the error above so we can help.
echo Python 3.12 must be installed on first setup.
pause
exit /b 1
