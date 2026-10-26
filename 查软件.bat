@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - when was this app / process used?
rem  Double-click and type a name, e.g.  pallas  /  weixin  /  WeGame
rem  Or pass it directly:  chapp.bat pallas
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" (
  "%~dp0TimeTrace.exe" app %*
) else (
  set "PY=py"
  where py >nul 2>nul || set "PY=python"
  "%PY%" "%~dp0timetrace.py" app %*
)
echo.
pause
endlocal
