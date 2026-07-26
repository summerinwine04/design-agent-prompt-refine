@echo off
chcp 65001 >nul
cd /d %~dp0
echo 推送 main 到 GitHub...
git push origin main
echo.
echo 完成（如上方无报错即推送成功）
pause
