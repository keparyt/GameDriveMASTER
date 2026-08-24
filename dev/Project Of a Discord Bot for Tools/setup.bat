@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   KEPAR LAB ASSIST - Docker Setup
echo ========================================
echo.

if not exist "config.py" (
    echo [ERROR] config.py was not found.
    echo.
    echo Create config.py from config.py.example and fill in your secrets.
    echo.
    pause
    exit /b 1
)

echo [OK] config.py found.
echo.
echo Building and starting the Discord bot...
echo.
docker compose -f compose.yml up -d --build
if errorlevel 1 (
    echo.
    echo [ERROR] Docker Compose failed.
    pause
    exit /b 1
)

echo.
echo [OK] Bot started.
echo.
docker compose ps
pause
