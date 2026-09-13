@echo off
chcp 936 >nul
title Build EXE - 运满满抢单机器人 (onedir)
cd /d "%~dp0.."

set DIST=dist\运满满抢单机器人
set NOBUILD=%1

echo ============================================================
echo   运满满抢单机器人 - 打包 [PyInstaller onedir + UAC 管理员]
echo   项目根: %CD%
echo ============================================================
echo.

if /i "%NOBUILD%"=="nobuild" (
    echo [跳过] 不重新编译，只重做资产拷贝（用于快速复验）
    goto assets
)

echo [1/5] 检查 PyInstaller ...
py -3.11 -m PyInstaller --version >nul 2>nul
IF ERRORLEVEL 1 (
    echo       未检测到，正在安装 PyInstaller ...
    py -3.11 -m pip install pyinstaller
    IF ERRORLEVEL 1 (
        echo       [失败] 安装 PyInstaller 失败，请检查网络/Python 环境。
        pause & exit /b 1
    )
)
for /f "delims=" %%v in ('py -3.11 -m PyInstaller --version') do echo       PyInstaller %%v  OK

echo [2/5] 清理旧产物 ...
if exist build rmdir /s /q build
if exist "%DIST%" rmdir /s /q "%DIST%"

echo [3/5] 编译中（首次 5~15 分钟，请勿中断）...
py -3.11 -m PyInstaller installer\build_exe.spec --noconfirm --clean
IF ERRORLEVEL 1 (
    echo       [失败] 编译失败，请查看上方报错。
    pause & exit /b 1
)

:assets
IF NOT EXIST "%DIST%\运满满抢单机器人.exe" (
    echo       [失败] 产物缺失: %DIST%\运满满抢单机器人.exe
    pause & exit /b 1
)
IF NOT EXIST "%DIST%\校准工具.exe" (
    echo       [失败] 产物缺失: %DIST%\校准工具.exe（标定/探活工具入口）
    pause & exit /b 1
)

echo [4/5] 拷贝运行时资产到 exe 同级 ...
rem config 必须与 exe 同级：core/config_store.py 在 frozen 下按 exe 目录找它
if exist "%DIST%\config" rmdir /s /q "%DIST%\config"
xcopy config "%DIST%\config\" /e /i /q /y >nul
echo       config             业务配置，路线/参数/标定都在这里，可改
rem data\templates 是识别资产，漏了会识别全废
if exist "%DIST%\data\templates" rmdir /s /q "%DIST%\data\templates"
xcopy "data\templates" "%DIST%\data\templates\" /e /i /q /y >nul
echo       data\templates     模板图，识别依赖，勿删

rem tools 必须放 exe 同级：工具用 Path(__file__).parent.parent 当项目根去找 config/
if exist "%DIST%\tools" rmdir /s /q "%DIST%\tools"
xcopy tools "%DIST%\tools\" /e /i /q /y >nul
echo       tools              标定/探活工具脚本，可编辑，改完存盘即生效（无需重打包）

echo [5/5] 建空的运行产物目录 + 启动器 ...
for %%d in (logs shots traces neg_frames) do mkdir "%DIST%\data\%%d" 2>nul
copy /y installer\launcher.bat "%DIST%\启动.bat" >nul
echo       已生成 启动.bat

echo.
echo ============================================================
echo   完成！交付目录: %DIST%
echo.
echo   - 整个文件夹拷到目标电脑即可用，目标机无需装 Python
echo   - 双击 运满满抢单机器人.exe 或 启动.bat 启动，会弹一次 UAC
echo   - 做标定/量下压: 双击 校准工具.exe（控制台菜单，不提权）
echo   - 改参数或路线看 config 目录；日志在 data\logs；截图在 data\shots
echo   - tools\ 是标定脚本本体，改判据存盘即生效；它与 exe 同级，勿删
echo   - 目标机还需要: iMouse 控制台 D:\iMousePro\iMouseManager.exe 加 手机投屏在线
echo ============================================================
pause
