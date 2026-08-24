@echo off
setlocal
cd /d "%~dp0"
echo ========================================
echo   KEPAR LAB ASSIST - Docker Update
echo ========================================
echo.
echo Rebuilding and restarting the bot...
echo.
docker compose -f compose.yml up -d --build
if errorlevel 1 (
    echo.
    echo [ERROR] Docker Compose update failed.
    pause
    exit /b 1
)
echo.
echo [OK] Bot updated and running.
echo.
docker compose ps
pause
