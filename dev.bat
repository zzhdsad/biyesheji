@echo off
chcp 65001 >nul
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Docker，请先启动 Docker Desktop，再双击本脚本。
  pause
  exit /b 1
)

if "%1"=="down" (
  echo 正在切回正式容器（使用镜像内代码）...
  docker compose -p biyesheji -f docker-compose.yml up -d backend frontend
  echo 已切回正式容器。要让改动发布，请先运行 build.ps1。
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

echo 正在以开发模式启动（后端 uvicorn --reload，前端 next dev）...
docker compose -p biyesheji -f docker-compose.yml -f docker-compose.dev.yml up -d backend frontend
if errorlevel 1 (
  echo [错误] 开发模式启动失败，请查看上方输出。
  pause
  exit /b 1
)

REM 容器重建会丢失镜像外 pip 安装的包（openpyxl / charset-normalizer 等），
REM 这里在启动后自动补齐：已存在则秒过，缺失才联网安装。
echo 正在校验后端依赖完整性...
docker compose -p biyesheji -f docker-compose.yml exec backend python -c "import openpyxl, charset_normalizer" >nul 2>&1
if errorlevel 1 (
  echo 缺少 openpyxl / charset-normalizer，正在安装（仅容器内，不改动镜像）...
  docker compose -p biyesheji -f docker-compose.yml exec backend pip install -q openpyxl charset-normalizer
)

echo.
echo 开发模式已启动：
echo   后端  http://localhost:8000/docs     改 backend/src 下代码即时生效
echo   前端  http://localhost:3000          改 frontend/src 下代码即时生效
echo.
echo 常用命令：
echo   dev.bat logs backend            查看后端实时日志
echo   dev.bat test tests -k 关键词    运行后端测试
echo   dev.bat migrate upgrade head    执行数据库迁移
echo   dev.bat down                    切回正式容器
echo.
pause
