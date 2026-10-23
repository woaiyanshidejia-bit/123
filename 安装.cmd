@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto folder_failed
if not exist "requirements.lock" goto package_missing
if not exist "wheels\openpyxl-3.1.5-py2.py3-none-any.whl" goto package_missing
if not exist "wheels\et_xmlfile-2.0.0-py3-none-any.whl" goto package_missing
py -3.12 -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto install_with_launcher
python -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)" >nul 2>&1
if not errorlevel 1 goto install_with_path
echo 请先安装 Python 3.12 的 64 位版本，勾选 Add Python to PATH。
echo 官方下载：https://www.python.org/downloads/windows/
echo 安装后关闭本窗口，重新双击 安装.cmd。
set "YINGBA_EXIT=2"
goto finish
:install_with_launcher
py -3.12 -m venv ".venv"
if errorlevel 1 goto install_failed
goto install_dependencies
:install_with_path
python -m venv ".venv"
if errorlevel 1 goto install_failed
:install_dependencies
".venv\Scripts\python.exe" -c "import sys, struct; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 else 1)"
if errorlevel 1 goto install_failed
".venv\Scripts\python.exe" -m pip --isolated install --no-index --no-cache-dir --force-reinstall --require-hashes --find-links "wheels" -r "requirements.lock"
if errorlevel 1 goto install_failed
echo 安装完成。以后请双击 启动.cmd。
echo 本次依赖均从解压包安装，不需要联网下载。
set "YINGBA_EXIT=0"
goto finish
:package_missing
echo 交付包不完整。请先解压整个 ZIP，再运行本脚本。
echo 请保留 wheels 文件夹及 requirements.lock。
set "YINGBA_EXIT=3"
goto finish
:install_failed
echo 安装失败。请保留上方错误文字，检查目录可写及交付包完整性。
echo 请勿提供账号密码、API Key、令牌或认证文件。
set "YINGBA_EXIT=1"
goto finish
:folder_failed
echo 无法进入程序目录。请把完整 ZIP 解压到您有写入权限的文件夹。
pause
exit /b 4
:finish
popd
pause
exit /b %YINGBA_EXIT%
