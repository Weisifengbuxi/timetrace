@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - print one day as a text log (default: latest day)
rem  Usage:  dangtian.bat
rem          dangtian.bat --date 2026-10-25
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" (
  "%~dp0TimeTrace.exe" day %*
) else (
  set "PY=py"
  where py >nul 2>nul || set "PY=python"
  "%PY%" "%~dp0timetrace.py" day %*
)
echo.
pause
endlocal
