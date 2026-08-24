@echo off
setlocal
cd /d "%~dp0"
echo KEPAR LAB ASSIST - Docker Status
echo.
docker compose -f compose.yml ps
echo.
pause
