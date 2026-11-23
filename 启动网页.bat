@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 春风 · 本地 Web 服务
echo 仅限本机使用，没有登录鉴权，请勿穿透到公网。
python run_web.py
if errorlevel 1 pause
pause