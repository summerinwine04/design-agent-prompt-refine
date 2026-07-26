@echo off
rem ============================================================
rem One-click launcher: backend(8000) + frontend-dev(5173) + ngrok
rem NOTE: keep this file ASCII-only. cmd parses .bat files with
rem the GBK codepage on this machine; UTF-8 Chinese text garbles
rem and breaks commands (that was the original bug, along with
rem broken nested quotes on the start line).
rem ============================================================
title launcher
cd /d "%~dp0"

echo [1/3] starting backend on http://127.0.0.1:8000 ...
start "backend-8000" cmd /k "cd /d %~dp0 && .venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000"

echo [2/3] starting frontend dev on http://localhost:5173 ...
start "frontend-5173" cmd /k "cd /d %~dp0frontend && npm run dev"

timeout /t 5 /nobreak >nul

echo [3/3] starting ngrok tunnel (public access) ...
start "ngrok" "%~dp0start_tunnel.bat"

timeout /t 3 /nobreak >nul
start "" http://localhost:5173/

echo.
echo 3 windows started: backend-8000 / frontend-5173 / ngrok
echo   Local  : http://localhost:5173
echo   Public : https://switch-refold-lapel.ngrok-free.dev/fitting-room
echo If a port is "already in use", that service was already
echo running - just close the extra window.
pause
