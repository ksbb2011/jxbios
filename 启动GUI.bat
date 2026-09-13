@echo off
rem 用 pythonw.exe（无控制台窗口）启动 GUI，不弹黑色终端
cd /d "%~dp0"
start "" "C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\pythonw.exe" gui\app.py
