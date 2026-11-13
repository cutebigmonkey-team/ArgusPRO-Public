@echo off
"%~dp0tools\pgsql\bin\pg_ctl.exe" -D "%~dp0postgres" -m fast -w stop
pause
