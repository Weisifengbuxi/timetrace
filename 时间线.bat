@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - open the timeline page (all recorded days)
rem  Extra args pass through, e.g.:
rem      shijianxian.bat --days 7
rem      shijianxian.bat --from 2026-10-01 --to 2026-10-31
rem      shijianxian.bat --no-open
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" (
  "%~dp0TimeTrace.exe" html %*
) else (
  set "PY=py"
  where py >nul 2>nul || set "PY=python"
  "%PY%" "%~dp0timetrace.py" html %*
)
if errorlevel 1 pause
endlocal
