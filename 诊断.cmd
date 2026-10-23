@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto folder_failed
if not exist ".venv\Scripts\python.exe" goto install_required
".venv\Scripts\python.exe" -m yingba.server --diagnose
set "YINGBA_EXIT=%ERRORLEVEL%"
if not "%YINGBA_EXIT%"=="0" echo 诊断未正常运行，请保留上方错误文字。
echo 分享诊断结果时不要附带账号密码、API Key、令牌或认证文件。
goto finish
:install_required
echo 请先双击 安装.cmd，完成后再运行诊断。
set "YINGBA_EXIT=2"
goto finish
:folder_failed
echo 无法进入程序目录。请先完整解压 ZIP 到您有写入权限的文件夹。
pause
exit /b 4
:finish
popd
pause
exit /b %YINGBA_EXIT%
