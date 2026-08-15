@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo Game Library - Reset Database
echo ========================================
echo.
echo This will permanently delete the local game database.
echo Artwork files are NOT deleted.
echo.
choice /C YN /M "Continue"
if errorlevel 2 exit /b 0

if exist "data\database.db" del /f /q "data\database.db"
if exist "data\database.db-shm" del /f /q "data\database.db-shm"
if exist "data\database.db-wal" del /f /q "data\database.db-wal"

echo.
echo Database reset successfully.
echo Start launcher.py again to rebuild the database.
echo.
pause
