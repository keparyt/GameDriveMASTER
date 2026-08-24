@echo off
setlocal
cd /d "%~dp0"
echo Starting KEPAR LAB ASSIST...
docker compose -f compose.yml up -d
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to start the bot.
    pause
    exit /b 1
)
echo.
echo [OK] Bot is running.
docker compose ps
pause
