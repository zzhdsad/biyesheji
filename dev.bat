@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM All messages are ASCII on purpose: non-ASCII lines in a UTF-8 .bat can be
REM split and executed as commands by cmd.exe on Chinese Windows.
REM Docker Desktop hosts PG / Redis / Milvus / MinIO. Wait up to 120s for the
REM engine instead of failing when it is still starting.
set /a DOCKER_TRIES=0
:wait_docker
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready
set /a DOCKER_TRIES+=1
if %DOCKER_TRIES% geq 60 goto docker_timeout
if %DOCKER_TRIES%==1 echo Waiting for Docker engine (up to 120s)...
timeout /t 2 /nobreak >nul
goto wait_docker

:docker_timeout
echo [ERROR] Docker is not ready. Start Docker Desktop, then run this script again.
pause
exit /b 1

:docker_ready

if "%1"=="down" (
  echo Switching back to production containers. Code is baked into images.
  docker compose -p biyesheji -f docker-compose.yml up -d backend frontend
  echo Done. Run build.ps1 first if you want code changes published.
  pause
  exit /b 0
)

if "%1"=="test" (
  docker compose -p biyesheji -f docker-compose.yml exec backend pytest %2 %3 %4 %5 %6 %7 %8 %9
  pause
  exit /b 0
)

if "%1"=="migrate" (
  docker compose -p biyesheji -f docker-compose.yml exec backend alembic %2 %3 %4 %5
  pause
  exit /b 0
)

if "%1"=="logs" (
  docker compose -p biyesheji -f docker-compose.yml logs -f %2
  exit /b 0
)

echo Starting dev mode (backend uvicorn --reload, frontend next dev, infra included)...
docker compose -p biyesheji -f docker-compose.yml -f docker-compose.dev.yml up -d
if errorlevel 1 (
  echo [ERROR] Dev mode failed to start. Check the output above.
  pause
  exit /b 1
)

REM Rebuilt containers lose pip packages installed outside the image
REM (openpyxl / charset-normalizer). Install only if missing.
echo Checking backend dependencies...
docker compose -p biyesheji -f docker-compose.yml exec backend python -c "import openpyxl, charset_normalizer" >nul 2>&1
if errorlevel 1 (
  echo Missing openpyxl / charset-normalizer, installing inside container only...
  docker compose -p biyesheji -f docker-compose.yml exec backend pip install -q openpyxl charset-normalizer
)

REM Warm up Next dev routes so the first click does not wait for compilation.
REM The script has per-route and total time bounds, so this step always ends
REM even when a cold compile is slow.
if exist "%~dp0scripts\warmup-frontend.ps1" (
  echo Warming up frontend routes. First start may take 1-2 minutes; progress is shown below.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\warmup-frontend.ps1"
  if errorlevel 1 echo [WARN] Warmup did not finish cleanly. The UI still works, just slower on first visit.
)

echo.
echo Dev mode is running:
echo   Backend  http://localhost:8000/docs     edits in backend/src apply instantly
echo   Frontend http://localhost:3000          edits in frontend/src apply instantly
echo.
echo Common commands:
echo   dev.bat logs backend            tail backend logs
echo   dev.bat test tests -k keyword   run backend tests
echo   dev.bat migrate upgrade head    run DB migrations
echo   dev.bat down                    switch back to production containers
echo.
echo Startup is complete. Containers keep running in the background.
echo Press any key to close this window.
pause
