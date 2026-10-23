@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
 echo 请先双击 安装.cmd，完成后再运行诊断。
 pause
 exit /b 1
)
.venv\Scripts\python.exe -m yingba.server --diagnose
pause
