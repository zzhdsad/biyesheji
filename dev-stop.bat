@echo off
chcp 65001 >nul
cd /d "%~dp0"

docker info >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Docker，请先启动 Docker Desktop。
  pause
  exit /b 1
)

echo 正在切回正式容器（使用镜像内代码）...
docker compose -p biyesheji -f docker-compose.yml up -d backend frontend
if errorlevel 1 (
  echo [错误] 切回失败，请查看上方输出。
  pause
  exit /b 1
)
echo 已切回正式容器。要让改动生效并发布，请先运行 build.ps1 重新构建镜像。
pause
