@echo off
chcp 65001 >nul
"%~dp0tools\python\python.exe" "%~dp0app\manage-users.py" --root "%~dp0."
pause
