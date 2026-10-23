@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0" >nul 2>&1
if errorlevel 1 goto folder_failed
if defined OPENAI_API_KEY goto api_config_blocked
if defined CODEX_API_KEY goto api_config_blocked
if defined OPENAI_BASE_URL goto api_config_blocked
if defined OPENAI_API_BASE goto api_config_blocked
if defined AZURE_OPENAI_API_KEY goto api_config_blocked
if defined CODEX_MODEL_PROVIDER goto api_config_blocked
if defined CODEX_ACCESS_TOKEN goto api_config_blocked
where codex >nul 2>&1
if errorlevel 1 goto cli_missing
call codex -c forced_login_method=chatgpt login
set "YINGBA_EXIT=%ERRORLEVEL%"
if not "%YINGBA_EXIT%"=="0" echo 登录未完成。请保留错误类型，按官方文档检查 CLI 版本或网络。
echo 登录由官方 Codex CLI 管理。请勿发送或复制认证文件、Cookie 或令牌。
goto finish
:cli_missing
echo 未找到官方 Codex CLI。请先按 docs\使用指南.md 安装，再重新打开本脚本。
echo 官方文档：https://developers.openai.com/codex/cli/
set "YINGBA_EXIT=2"
goto finish
:api_config_blocked
echo 检测到 API 或自定义认证环境配置，本工具只接受 ChatGPT 订阅登录。
echo 请在独立的干净终端中运行，不要将 API Key 或令牌发给维护者。
set "YINGBA_EXIT=3"
goto finish
:folder_failed
echo 无法进入程序目录。请先完整解压 ZIP 到您有写入权限的文件夹。
pause
exit /b 4
:finish
popd
pause
exit /b %YINGBA_EXIT%
