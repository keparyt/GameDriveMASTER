@echo off
setlocal
cd /d "%~dp0"
echo Stopping KEPAR LAB ASSIST...
echo.
docker compose -f compose.yml down
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to stop the bot.
    pause
    exit /b 1
)
echo.
echo [OK] Bot stopped.
pause
