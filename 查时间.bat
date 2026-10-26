@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - which app was in use at a given moment?
rem  Double-click and type e.g.  14:30  or  2026-10-26 14:30
rem  Or pass it directly:  chatime.bat 14:30
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" (
  "%~dp0TimeTrace.exe" at %*
) else (
  set "PY=py"
  where py >nul 2>nul || set "PY=python"
  "%PY%" "%~dp0timetrace.py" at %*
)
echo.
pause
endlocal
