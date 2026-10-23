@echo off
chcp 65001 >nul
where codex >nul 2>&1
if errorlevel 1 (
 echo 请先按使用指南安装官方 Codex CLI。
 pause
 exit /b 1
)
codex login
pause
