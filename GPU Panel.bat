@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
if not exist "%PYW%" set "PYW=python.exe"
start "" "%PYW%" "%~dp0gpu_monitor.py"
