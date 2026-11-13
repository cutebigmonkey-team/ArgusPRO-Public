@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-Argus.ps1" -TestArchive
if errorlevel 1 pause
