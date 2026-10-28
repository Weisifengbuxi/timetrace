@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - one entry point. Shows a small menu:
rem    1) timeline page   2) what was used at a time
rem    3) when was an app used   4) USB records   5) day log
rem  Command line still works:  TimeTrace.exe html / at / app / events
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" --menu %*
goto done
:runpy
py -3 "%~dp0timetrace.py" --menu %*
goto done
:runexe
"%~dp0TimeTrace.exe" --menu %*
:done
if errorlevel 1 pause
endlocal
