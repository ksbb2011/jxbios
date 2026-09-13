@echo off
REM ============================================================
REM  UI tap calibration launcher (the menu itself lives in Python)
REM
REM  Why this file is ASCII-only: putting Chinese text inside a .bat
REM  makes cmd mis-parse it (file encoding + full-width parentheses),
REM  which ate whole menu lines on 2026-09-13. Python prints the
REM  Chinese menu correctly under chcp 65001, so the .bat stays a
REM  plain launcher.
REM
REM  Tools:
REM    tools\calibrate_tap_ui.py  --menu   interactive menu
REM    tools\calibrate_tap_ui.py  --list   target list + setup notes
REM ============================================================

cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%CD%"

py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python launcher "py -3.11" not found.
    echo          Please install Python 3.11 x64 first.
    echo.
    pause
    exit /b 1
)

py -3.11 tools\calibrate_tap_ui.py --menu
if errorlevel 1 (
    echo.
    echo  [ERROR] tool exited with an error - see messages above.
    pause
)
exit /b 0
