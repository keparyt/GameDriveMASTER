@echo off
setlocal
cd /d "%~dp0"
echo Showing KEPAR LAB ASSIST logs...
echo Press Ctrl+C to stop viewing logs.
echo.
docker compose -f compose.yml logs -f
pause
