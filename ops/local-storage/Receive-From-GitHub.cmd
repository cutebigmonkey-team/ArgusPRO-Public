@echo off
cd /d "%~dp0"
"%~dp0tools\python\python.exe" "%~dp0app\storage.py" --root "%~dp0." receive
if errorlevel 1 echo Import was not completed. See the message above.
pause
