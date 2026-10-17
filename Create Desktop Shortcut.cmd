@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Create Desktop Shortcut.ps1"
if errorlevel 1 (
    echo The shortcut could not be created. Read the error above.
    pause
    exit /b 1
)
echo.
echo Done! Double-click Kalshi Bot (Demo) on your desktop.
pause
