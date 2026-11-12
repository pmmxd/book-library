@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if /I "%~1"=="start" goto start
if /I "%~1"=="stop" goto stop
if /I "%~1"=="restart" goto restart
if /I "%~1"=="status" goto status
if not "%~1"=="" goto usage

:menu
echo.
echo Book Library Service
echo 1. Start
echo 2. Stop
echo 3. Restart
echo 4. Status
echo 5. Exit
choice /C 12345 /N /M "Choose [1-5]: "
if errorlevel 5 exit /b 0
if errorlevel 4 goto status
if errorlevel 3 goto restart
if errorlevel 2 goto stop
goto start

:start
call "%~dp0start_library.cmd"
exit /b %ERRORLEVEL%

:stop
call "%~dp0stop_library.cmd"
exit /b %ERRORLEVEL%

:restart
call "%~dp0stop_library.cmd" >nul 2>nul
call "%~dp0start_library.cmd"
exit /b %ERRORLEVEL%

:status
if not exist "%~dp0..\data\runtime\server.pid" goto not_running
for /f "usebackq delims=" %%P in ("%~dp0..\data\runtime\server.pid") do set "SERVER_PID=%%P"
if not defined SERVER_PID goto not_running
tasklist /FI "PID eq %SERVER_PID%" /NH 2>nul | findstr /R /C:"[ ]%SERVER_PID%[ ]" >nul
if errorlevel 1 goto not_running
if not exist "%~dp0..\data\runtime\server.port" goto running_no_port
for /f "usebackq delims=" %%P in ("%~dp0..\data\runtime\server.port") do set "SERVER_PORT=%%P"
echo Running
echo PID: %SERVER_PID%
echo URL: http://127.0.0.1:%SERVER_PORT%/
exit /b 0

:running_no_port
echo Running
echo PID: %SERVER_PID%
echo Port file is missing.
exit /b 1

:not_running
echo Not running.
exit /b 1

:usage
echo Usage: library_service.cmd [start^|stop^|restart^|status]
exit /b 1
