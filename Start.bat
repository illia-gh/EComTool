@echo off
setlocal EnableExtensions

rem Always run from repository root, even when launched by double-click.
cd /d "%~dp0"

set "VENV_DIR=%CD%\.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "APP_URL=http://127.0.0.1:8000"
set "APP_HOST=127.0.0.1"
set "APP_PORT=8000"
set "PID_FILE=%CD%\outputs\runtime\ecomtool-server.pid"
set "STOP_FILE=%CD%\outputs\runtime\ecomtool-stop-requested"
set "LISTENER_PID="

rem Idempotent restart: stop an existing launcher-owned server before starting.
for /f "tokens=5" %%P in ('netstat -ano -p TCP ^| findstr /C:"%APP_HOST%:%APP_PORT%" ^| findstr "LISTENING"') do (
    if not defined LISTENER_PID set "LISTENER_PID=%%P"
)

if defined LISTENER_PID (
    echo Existing listener found on %APP_HOST%:%APP_PORT%. Restarting EComTool...
    call "%~dp0Stop.bat" --restart
    if errorlevel 1 (
        echo ERROR: Existing listener could not be stopped safely.
        goto :fail
    )
)

if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found in %CD%
    goto :fail
)

if exist "%VENV_PY%" goto :venv_ready

echo Creating Python virtual environment...
call :find_python
if not defined PY_CMD (
    echo ERROR: No real Python interpreter found.
    echo The Microsoft Store "python" alias is a stub, not a real interpreter.
    echo Install Python 3.12+ from https://www.python.org/downloads/windows/
    echo During setup, tick "Add python.exe to PATH".
    goto :fail
)
echo Using interpreter: %PY_CMD%
%PY_CMD% -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo ERROR: Failed to create virtual environment.
    goto :fail
)

:venv_ready

echo Installing or updating dependencies...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo ERROR: Dependency installation failed.
    goto :fail
)

set "PYTHONPATH=%CD%\tool\scripts;%CD%\tool"
if not exist "%CD%\outputs\runtime" mkdir "%CD%\outputs\runtime"
if exist "%PID_FILE%" del /q "%PID_FILE%"
if exist "%STOP_FILE%" del /q "%STOP_FILE%"

echo.
echo Starting EComTool at %APP_URL%
echo Keep this window open. Press Ctrl+C to stop server.
echo.

set "OPEN_BROWSER=1"
if defined ECOMTOOL_NO_BROWSER set "OPEN_BROWSER=0"

rem Wait until Uvicorn listens (app loaded), record its PID for Stop.bat,
rem then open browser. A fixed delay opened a blank page on slow starts.
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command ^
    "$deadline = (Get-Date).AddSeconds(120); " ^
    "do { " ^
    "  $line = netstat -ano -p TCP | Select-String '127\.0\.0\.1:8000.*LISTENING' | Select-Object -First 1; " ^
    "  if ($line -and $line.Line -match '\s+(\d+)\s*$') { Set-Content -Encoding ASCII '%PID_FILE%' $Matches[1]; if ('%OPEN_BROWSER%' -eq '1') { Start-Process '%APP_URL%' }; break }; " ^
    "  Start-Sleep -Milliseconds 250 " ^
    "} while ((Get-Date) -lt $deadline)"

"%VENV_PY%" -m uvicorn app:app --host 127.0.0.1 --port 8000
set "EXIT_CODE=%ERRORLEVEL%"
if exist "%PID_FILE%" del /q "%PID_FILE%"

if exist "%STOP_FILE%" (
    del /q "%STOP_FILE%"
    echo.
    echo EComTool server stopped by Stop.bat.
    endlocal
    exit /b 0
)

if not "%EXIT_CODE%"=="0" (
    echo.
    echo ERROR: Server stopped with exit code %EXIT_CODE%.
    goto :fail
)

endlocal
exit /b 0

:fail
echo.
pause
endlocal
exit /b 1

:find_python
rem Set PY_CMD to a real Python launch command, or leave it undefined.
rem Each candidate is probed by running real code, so the Microsoft Store
rem stub (prints a message and exits nonzero) is rejected automatically.
set "PY_CMD="
call :try_cmd "py -3"
for %%V in (314 313 312 311 310) do call :try_exe "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"
for %%V in (314 313 312 311 310) do call :try_exe "%ProgramFiles%\Python%%V\python.exe"
call :try_cmd "python"
goto :eof

:try_cmd
if defined PY_CMD goto :eof
%~1 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set "PY_CMD=%~1"
goto :eof

:try_exe
if defined PY_CMD goto :eof
if not exist "%~1" goto :eof
"%~1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto :eof
set PY_CMD="%~1"
goto :eof
