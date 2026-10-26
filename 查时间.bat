@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - which app was in use at a given moment?
rem  Double-click and type e.g.  14:30  or  2026-10-26 14:30
rem  Or pass it directly:  chatime.bat 14:30
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" at %*
goto done
:runpy
py -3 "%~dp0timetrace.py" at %*
goto done
:runexe
"%~dp0TimeTrace.exe" at %*
:done
echo.
pause
endlocal
