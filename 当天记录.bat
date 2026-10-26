@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - print one day as a text log (default: latest day)
rem  Usage:  dangtian.bat
rem          dangtian.bat --date 2026-10-25
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" day %*
goto done
:runpy
py -3 "%~dp0timetrace.py" day %*
goto done
:runexe
"%~dp0TimeTrace.exe" day %*
:done
echo.
pause
endlocal
