@echo off
rem mask-tool 桌面版启动（pywebview 原生窗口，无需浏览器）
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m mask_tool.desktop
if errorlevel 1 (
  echo.
  echo [启动失败] 常见原因：
  echo   1. WebView2 运行时缺失：https://developer.microsoft.com/microsoft-edge/webview2/
  echo   2. 依赖未安装：pip install -e ".[web,desktop]"
  pause
)
