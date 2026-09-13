@echo off
chcp 65001 >nul
setlocal
title 安装运行依赖 - 运满满抢单机器人

REM ============================================================
REM  新电脑「源码路线」一键装依赖（exe 路线不需要跑这个）
REM  1) 校验 Python 3.11   2) 装 requirements.txt
REM  3) 逐包 import 自检（含 OCR 那条链）
REM
REM  注意：本文件是 UTF-8 + chcp 65001。按 2026-09-13 踩过的坑，
REM  echo 里**不要写全角括号**（会被 cmd 误解析、整行吃掉），只用 ASCII 括号。
REM ============================================================

cd /d "%~dp0"
set "MIRROR="
if /i "%~1"=="mirror" set "MIRROR=-i https://pypi.tuna.tsinghua.edu.cn/simple"

echo ============================================================
echo   运满满抢单机器人 - 依赖安装 / 环境自检
echo   项目根: %CD%
echo ============================================================
echo.

echo [1/4] 检查 Python 3.11 ...
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [错误] 找不到 py -3.11 启动器。
    echo.
    echo   请先安装 Python 3.11.x 64 位，安装时**务必勾选**:
    echo     - Add python.exe to PATH
    echo     - py launcher
    echo   下载地址: https://www.python.org/downloads/release/python-3119/
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('py -3.11 --version') do echo       %%v  OK

echo.
echo [2/4] 升级 pip ...
py -3.11 -m pip install --upgrade pip %MIRROR%
if errorlevel 1 (
    echo       [警告] pip 升级失败，继续尝试安装依赖。
)

echo.
echo [3/4] 安装 requirements.txt ...
echo       首次约 3~8 分钟（onnxruntime / opencv / PyQt5 体积较大）。
if defined MIRROR echo       使用清华镜像: %MIRROR%
py -3.11 -m pip install -r requirements.txt %MIRROR%
if errorlevel 1 (
    echo.
    echo   [失败] 依赖安装失败。常见原因:
    echo     - 网络不通: 重跑 install.bat mirror 走清华镜像
    echo     - Python 版本不对: 必须 3.11.x 64 位
    echo.
    pause
    exit /b 1
)

echo.
echo [4/4] 逐包 import 自检（含 OCR 链）...
py -3.11 tools\check_deps.py
if errorlevel 1 (
    echo.
    echo   [注意] 上面列出了装不上的包。用同一个 Python 手动重试:
    echo          py -3.11 -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   完成！环境已就绪。
echo.
echo   下一步:
echo     - 跑程序: 双击 启动GUI.bat
echo     - 做标定: 双击 标定工具.bat
echo     - 装环境之外还要装的外部组件见 docs\新电脑_环境安装配置.md
echo ============================================================
pause
