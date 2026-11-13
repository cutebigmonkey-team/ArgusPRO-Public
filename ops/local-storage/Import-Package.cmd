@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag an Argus package ZIP onto this file to import it.
) else (
  "%~dp0tools\python\python.exe" "%~dp0app\storage.py" --root "%~dp0." import "%~1"
)
pause
