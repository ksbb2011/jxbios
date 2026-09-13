@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ============================================================
REM  机械臂标定工具  ——  双击运行，按数字选择
REM  项目：robot_order_picker_imouse（iMouse 投屏版）
REM
REM  本 bat 只负责：切目录 / 设环境 / 读配置 / 拼命令 / 调用 Python
REM  核心标定逻辑全在 tools\calibrate_arm_ink.py（已固化，本 bat 不修改它）
REM ============================================================

cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%CD%"
set "PY=py -3.11"

REM ---- 检查 Python
%PY% --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [错误] 找不到 py -3.11，请确认已安装 Python 3.11
    echo.
    pause
    exit /b 1
)

REM ---- 读取当前标定状态（z_press / actuation）
set "ZPRESS="
set "HAS_ACT=0"
if exist "config\hardware.json" (
    for /f "delims=" %%i in ('%PY% -c "import json;c=json.load(open('config/hardware.json',encoding='utf-8'));print(c.get('calibration',{}).get('z_press',''))" 2^>nul') do set "ZPRESS=%%i"
    for /f "delims=" %%i in ('%PY% -c "import json;c=json.load(open('config/hardware.json',encoding='utf-8'));print(1 if c.get('calibration',{}).get('actuation') else 0)" 2^>nul') do set "HAS_ACT=%%i"
)

:MENU
cls
echo.
echo  ================================================================
echo     机械臂标定工具     robot_order_picker_imouse
echo  ================================================================
echo.
echo     [当前状态]
if defined ZPRESS (echo       z_press   = %ZPRESS%) else (echo       z_press   = 未标定)
if "%HAS_ACT%"=="1" (echo       actuation = 已标定) else (echo       actuation = 未标定)
echo.
echo  ----------------------------------------------------------------
echo     1. 机械臂归位          （救急：卡住 / 串口被占时用）
echo     2. 测下压深度 z_press  （换笔、换手机后必做）
echo     3. 跑映射标定          （完整，3~10 分钟）
echo     4. 复检                （只验证精度，约 1 分钟，不重新标定）
echo     5. 单步探针            （戳一下看能否出墨点，排障第一步）
echo     6. iMouse 探活与实测
echo  ----------------------------------------------------------------
echo     0. 退出
echo  ================================================================
echo.
set "C="
set /p "C=请输入数字后回车: "

if "%C%"=="1" goto DO_HOME
if "%C%"=="2" goto DO_SCANZ
if "%C%"=="3" goto DO_CAL
if "%C%"=="4" goto DO_VERIFY
if "%C%"=="5" goto DO_PROBE
if "%C%"=="6" goto DO_PROBE_MOUSE
if "%C%"=="0" goto END
goto MENU

REM ============================================================
:DO_HOME
cls
echo  [1] 机械臂归位：抬笔 -^> 回原点 -^> 释放串口
echo      串口被占时会自动弹 UAC 重启 JxbService，请点"是"
echo  ------------------------------------------------------------
echo.
%PY% tools\arm_home.py
echo.
pause
goto MENU

REM ============================================================
:DO_SCANZ
cls
echo  [2] 测下压深度 z_press
echo      机械臂会从浅到深逐档下压，找到"最轻能触发触屏"的深度
echo      然后自动加上浮值（1~3mm，用户规范）
echo  ------------------------------------------------------------
echo.
set "UF="
set /p "UF=上浮值(1~3mm，直接回车用 1.5): "
if "%UF%"=="" set "UF=1.5"
echo.
%PY% tools\calibrate_arm_ink.py --scan-z --z-start 5.5 --z-max 8.0 --z-step 0.1 --up-float %UF%
echo.
pause
goto MENU

REM ============================================================
:DO_CAL
cls
echo  [3] 跑映射标定
echo      机械臂会在手机画面上戳网格点，用截图找墨迹重心，
echo      拟合双线性模型，最后用 10~16 个点验收（不达标不写入）
echo  ------------------------------------------------------------
echo.
if defined ZPRESS (echo  当前 z_press = %ZPRESS%) else (echo  当前 z_press = 未标定)
echo.
set "ZP="
set /p "ZP=直接回车用上面这个值，或输入新值: "
if "%ZP%"=="" set "ZP=%ZPRESS%"
if "%ZP%"=="" (
    echo.
    echo  [提示] 还没有 z_press，请先执行选项 2 测下压深度
    echo.
    pause
    goto MENU
)
set "GD="
set /p "GD=网格密度(6=36点约3分钟 / 8=64点约10分钟，回车用 8): "
if "%GD%"=="" set "GD=8"
set "RD="
set /p "RD=校准轮数(1=快 / 2=更准，回车用 2): "
if "%RD%"=="" set "RD=2"
echo.
echo  开始标定：z_press=%ZP%  grid=%GD%  rounds=%RD%
echo  手机请停在「备忘录 → 标记 → 第 3 个笔 → 空白画布」
echo.
pause
%PY% tools\calibrate_arm_ink.py --z-press %ZP% --grid %GD% --rounds %RD% --auto-restart
echo.
pause
goto MENU

REM ============================================================
:DO_VERIFY
cls
echo  [4] 复检（只验证精度，不重新标定）
echo      用已有 actuation 模型戳 16 个点，看误差是否仍 ≤3pt
echo  ------------------------------------------------------------
echo.
if not "%HAS_ACT%"=="1" (
    echo  [提示] 还没有 actuation 模型，请先执行选项 3 跑标定
    echo.
    pause
    goto MENU
)
%PY% tools\calibrate_arm_ink.py --verify-only
echo.
pause
goto MENU

REM ============================================================
:DO_PROBE
cls
echo  [5] 单步探针：在画布中心戳一下，看能否检测出墨点
echo      这是"检测不到墨点"类故障的排障第一步
echo  ------------------------------------------------------------
echo.
set "ZP2="
set /p "ZP2=z_press(回车用 %ZPRESS%): "
if "%ZP2%"=="" set "ZP2=%ZPRESS%"
if "%ZP2%"=="" (
    echo  [提示] 还没有 z_press，请先执行选项 2
    pause
    goto MENU
)
%PY% tools\calibrate_arm_ink.py --probe-one --z-press %ZP2%
echo.
pause
goto MENU

REM ============================================================
:DO_PROBE_MOUSE
cls
echo  [6] iMouse 探活与实测
echo      探测版本/端口、分辨率、截图耗时、投屏帧率、插件可用性
echo  ------------------------------------------------------------
echo.
%PY% tools\imouse_probe.py --repeat 30
echo.
echo  报告已写入 docs\实测报告_iMouse.md
echo.
pause
goto MENU

REM ============================================================
:END
endlocal
exit /b 0