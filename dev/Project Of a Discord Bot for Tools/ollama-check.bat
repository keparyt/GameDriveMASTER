@echo off
setlocal

echo ========================================
echo   Ollama Connectivity Check
 echo ========================================
echo.

echo [1/3] Checking local Ollama...
curl -s --connect-timeout 3 http://127.0.0.1:11434/api/tags
if errorlevel 1 echo [ERROR] Local Ollama is not reachable.
echo.

echo [2/3] Checking Docker host address...
curl -s --connect-timeout 3 http://host.docker.internal:11434/api/tags
if errorlevel 1 echo [ERROR] host.docker.internal:11434 is not reachable from this Windows environment.
echo.

echo [3/3] Docker containers...
cd /d "%~dp0"
docker compose -f compose.yml ps

echo.
echo NOTE:
echo Ollama must listen on an address reachable by Docker.
echo If Ollama only listens on 127.0.0.1:11434, the container cannot access it.
echo.
pause
