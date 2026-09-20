@echo off
title mask-tool
rem mask-tool desktop launcher (main entry): opens pywebview native window.
rem Launches the app via pythonw (no console): this window closes itself
rem right after firing the app; closing the app window exits everything,
rem leaving no background services behind.
rem For install/diagnostics run install-windows.bat. On startup failure the
rem app shows a native error dialog; details in
rem %LOCALAPPDATA%\mask-tool\desktop-error.log

cd /d "%~dp0"

if not exist "pyproject.toml" (
    echo [ERROR] Please run this script from the mask-tool project directory.
    pause
    exit /b 1
)

rem Preferred: venv pythonw (no console window)
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m mask_tool.desktop
    exit /b 0
)

rem Fallback: activate venv, then any pythonw on PATH
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw -m mask_tool.desktop
    exit /b 0
)

rem Last resort: console mode (window kept for troubleshooting)
echo Starting mask-tool desktop (console fallback)...
python -m mask_tool.desktop

if errorlevel 1 (
    echo.
    echo [FAILED] Common causes:
    echo   1. Dependencies missing: pip install -e ".[app]"
    echo   2. WebView2 runtime missing: https://developer.microsoft.com/microsoft-edge/webview2/
    echo   3. First time setup: run install-windows.bat first
    pause
)
