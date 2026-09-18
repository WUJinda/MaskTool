@echo off
chcp 65001 >nul 2>&1
title mask-tool 文件脱敏工具

rem ============================================
rem  mask-tool 桌面应用启动器（主入口）
rem  打开 pywebview 原生窗口，无需浏览器。
rem  关闭窗口即退出，无残留后台服务。
rem ============================================

cd /d "%~dp0"

if not exist "pyproject.toml" (
    echo 错误：请在 mask-tool 项目目录中运行此脚本
    pause
    exit /b 1
)

if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

echo 正在启动 mask-tool 桌面窗口...
python -m mask_tool.desktop

if errorlevel 1 (
    echo.
    echo [启动失败] 常见原因：
    echo   1. 依赖未安装：pip install -e ".[app]"
    echo   2. WebView2 运行时缺失：https://developer.microsoft.com/microsoft-edge/webview2/
    echo   3. 首次使用请先运行 install-windows.bat
    pause
)
