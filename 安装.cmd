@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3.12 --version >nul 2>&1
if errorlevel 1 (
 echo 请先安装 Python 3.12（64位），安装时勾选 Add Python to PATH。
 echo 官方下载：https://www.python.org/downloads/windows/
 pause
 exit /b 1
)
py -3.12 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install --no-index --find-links wheels -r requirements.lock
if errorlevel 1 goto fail
echo 安装完成。请双击 启动.cmd。
pause
exit /b 0
:fail
echo 安装失败，请将完整错误交给维护者；不需要提供账号密码。
pause
exit /b 1
