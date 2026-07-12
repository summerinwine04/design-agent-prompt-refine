@echo off
title ngrok updater
echo Updating ngrok agent to latest version...
echo.
ngrok update
echo.
echo Done. Version now:
ngrok version
echo.
pause
