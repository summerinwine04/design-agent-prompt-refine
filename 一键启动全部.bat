@echo off
chcp 65001 >nul
title 一键启动
echo [1/2] 启动后端 (端口 8000)...
start "backend-8000" cmd /k ""%~dp0.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --app-dir "%~dp0.""

timeout /t 5 /nobreak >nul

echo [2/2] 启动公网隧道...
start "ngrok" "%~dp0启动公网隧道.bat"

echo.
echo 两个窗口已拉起：
echo   - backend-8000: 后端（若提示端口被占用，说明后端本来就开着，直接关掉那个新窗口即可）
echo   - ngrok:        公网隧道
echo.
echo 公网地址: https://switch-refold-lapel.ngrok-free.dev/fitting-room
pause
