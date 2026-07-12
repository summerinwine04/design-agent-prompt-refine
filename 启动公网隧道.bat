@echo off
chcp 65001 >nul
title ngrok 公网隧道
echo ============================================
echo  公网地址: https://switch-refold-lapel.ngrok-free.dev
echo  转发到:   http://localhost:8000  (请确保后端已启动)
echo  关闭此窗口 = 断开公网访问
echo ============================================
echo.

rem 新版参数 --url 优先；老版本(3.3.x)自动回退 --domain
ngrok http --url=switch-refold-lapel.ngrok-free.dev 8000
if errorlevel 1 (
  echo.
  echo [提示] 尝试老版本参数 --domain ...
  ngrok http --domain=switch-refold-lapel.ngrok-free.dev 8000
)

echo.
echo 隧道已退出。若上方有 ERROR 信息，请截图。
pause
