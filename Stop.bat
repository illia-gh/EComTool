@echo off
setlocal EnableExtensions

rem Always resolve paths relative to repository root.
cd /d "%~dp0"

set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
set "PID_FILE=%CD%\outputs\runtime\ecomtool-server.pid"
set "STOP_FILE=%CD%\outputs\runtime\ecomtool-stop-requested"
set "SERVER_PID="
set "LISTENER_PID="
set "NO_PAUSE="

if /i "%~1"=="--restart" set "NO_PAUSE=1"

if exist "%PID_FILE%" set /p SERVER_PID=<"%PID_FILE%"

for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /C:"%APP_HOST%:%APP_PORT%" ^| findstr "LISTENING"') do (
    if not defined LISTENER_PID set "LISTENER_PID=%%P"
)

if not defined LISTENER_PID (
    if exist "%PID_FILE%" del /q "%PID_FILE%"
    if exist "%STOP_FILE%" del /q "%STOP_FILE%"
    echo EComTool server is not running on %APP_HOST%:%APP_PORT%.
    endlocal
    exit /b 0
)

rem Safety guard: kill only listener PID recorded by this project's Start.bat.
if not defined SERVER_PID (
    rem Restart can recover legacy/manual EComTool server after exact local UI check.
    if defined NO_PAUSE (
        curl.exe --silent --fail --max-time 3 "http://%APP_HOST%:%APP_PORT%/" 2>nul | findstr /C:"PV + BESS analysis" >nul
        if not errorlevel 1 (
            echo Confirmed existing EComTool server ^(PID %LISTENER_PID%^).
            set "SERVER_PID=%LISTENER_PID%"
        )
    )
)

if not defined SERVER_PID (
    echo REFUSING: port %APP_PORT% has a listener but launcher PID file is missing.
    echo Listener did not identify as EComTool started by this project.
    goto :fail
)

if not "%SERVER_PID%"=="%LISTENER_PID%" (
    echo REFUSING: launcher PID %SERVER_PID% does not match listener PID %LISTENER_PID%.
    if exist "%PID_FILE%" del /q "%PID_FILE%"
    goto :fail
)

echo Stopping EComTool server ^(PID %SERVER_PID%^)...
>"%STOP_FILE%" echo stop
taskkill /PID %SERVER_PID% /T /F >nul 2>nul
if errorlevel 1 (
    if exist "%STOP_FILE%" del /q "%STOP_FILE%"
    echo ERROR: Could not stop PID %SERVER_PID%.
    goto :fail
)

rem Wait until original Start.bat consumes stop marker and finishes cleanup.
for /l %%W in (1,1,5) do (
    if exist "%STOP_FILE%" ping.exe -n 2 127.0.0.1 >nul
)

if exist "%PID_FILE%" del /q "%PID_FILE%"
if exist "%STOP_FILE%" del /q "%STOP_FILE%"
echo EComTool server stopped. Run Start.bat to launch again.
endlocal
exit /b 0

:fail
echo.
if defined NO_PAUSE (
    endlocal
    exit /b 1
)
pause
endlocal
exit /b 1
