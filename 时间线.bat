@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - open the timeline page (all recorded days)
rem  Extra args pass through, e.g.:
rem      --days 7
rem      --from 2026-10-01 --to 2026-10-31
rem      --no-open
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" html %*
goto done
:runpy
py -3 "%~dp0timetrace.py" html %*
goto done
:runexe
"%~dp0TimeTrace.exe" html %*
:done
if errorlevel 1 pause
endlocal
