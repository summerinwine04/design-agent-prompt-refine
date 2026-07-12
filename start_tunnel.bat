@echo off
title ngrok tunnel
echo [1/3] Updating ngrok agent (account requires ^>= 3.20.0)...
ngrok update
echo.
echo [2/3] Version check:
ngrok version
echo.
echo [3/3] Starting tunnel: https://switch-refold-lapel.ngrok-free.dev  -^>  http://localhost:8000
echo Close this window = tunnel stops
echo.
ngrok http --url=switch-refold-lapel.ngrok-free.dev 8000
if errorlevel 1 (
  echo.
  echo [fallback] trying legacy flag --domain ...
  ngrok http --domain=switch-refold-lapel.ngrok-free.dev 8000
)
echo.
echo Tunnel exited. If there is an ERROR above, take a screenshot.
pause
