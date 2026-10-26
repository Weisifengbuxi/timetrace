@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - list USB drive (U disk) connection events
rem  Read from the Windows device log; no file contents, no serials.
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" events %*
goto done
:runpy
py -3 "%~dp0timetrace.py" events %*
goto done
:runexe
"%~dp0TimeTrace.exe" events %*
:done
echo.
pause
endlocal
