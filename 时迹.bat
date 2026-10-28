@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - start the tray icon in the notification area.
rem  Right-click it for: timeline / what was used at a time /
rem  when an app was used / USB records / day log / status.
rem  Command line still works: TimeTrace.exe html | at | app | events
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
start "" python "%~dp0timetrace.py" --tray
goto done
:runpy
start "" py -3 "%~dp0timetrace.py" --tray
goto done
:runexe
start "" "%~dp0TimeTrace.exe" --tray
:done
endlocal
