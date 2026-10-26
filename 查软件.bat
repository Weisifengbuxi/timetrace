@echo off
rem ---------------------------------------------------------------
rem  TimeTrace - when was this app / process used?
rem  Double-click and type a name, e.g.  pallas  /  weixin  /  WeGame
rem  Or pass it directly:  chapp.bat pallas
rem ---------------------------------------------------------------
chcp 65001 >nul
setlocal
if exist "%~dp0TimeTrace.exe" goto runexe
where py >nul 2>nul && goto runpy
python "%~dp0timetrace.py" app %*
goto done
:runpy
py -3 "%~dp0timetrace.py" app %*
goto done
:runexe
"%~dp0TimeTrace.exe" app %*
:done
echo.
pause
endlocal
