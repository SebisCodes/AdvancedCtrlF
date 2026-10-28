@echo off
rem =====================================================================
rem  Advanced Ctrl+F - launcher for Windows
rem
rem  Python is looked up in this order:
rem    1. PYTHON_BIN (set below), if given
rem    2. a python.exe inside the .\python\ subfolder
rem    3. an archive in .\python\ (.zip / .tar.gz / .tgz) -> extracted,
rem       or an official Python installer (python-*.exe) -> installed
rem       silently into .\python\installed
rem    4. download of the latest stable official CPython (Python Software
rem       Foundation NuGet package; SHA512 + digital signature verified)
rem    5. fallback: system Python (py -3 / python) in its own venv
rem
rem  Usage:  start.cmd           start normally (no console window)
rem          start.cmd debug     with console (errors are visible)
rem          start.cmd setup     set up only
rem =====================================================================
setlocal EnableExtensions

rem ---- Settings -------------------------------------------------------
rem set "PYTHON_BIN=C:\path\to\python.exe"
rem set "PY_VERSION=3.13"
rem ---------------------------------------------------------------------

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "PY_DIR=%ROOT%\python"

rem Launcher messages, the Python download and the package installation are also
rem written to data\setup.log (kept below ~1 MB) - useful when something goes wrong
if not exist "%ROOT%\data" mkdir "%ROOT%\data"
set "SETUP_LOG=%ROOT%\data\setup.log"
if exist "%SETUP_LOG%" for %%F in ("%SETUP_LOG%") do if %%~zF GTR 1000000 move /y "%SETUP_LOG%" "%SETUP_LOG%.1" >nul
chcp 65001 >nul
>>"%SETUP_LOG%" echo ===== %DATE% %TIME% start.cmd %* ^| %PROCESSOR_ARCHITECTURE% ^| %ROOT%
>>"%SETUP_LOG%" ver
if not exist "%PY_DIR%" mkdir "%PY_DIR%"
set "PY="

if defined PYTHON_BIN (
    if exist "%PYTHON_BIN%" ( set "PY=%PYTHON_BIN%" ) else (
        call :log PYTHON_BIN points to a file that does not exist: %PYTHON_BIN%
        goto :error
    )
)
call :locate_python
if defined PY call :check_python
if not defined PY (
    call :log No Python found. Put a Python or an archive into "%PY_DIR%"
    call :log or set PYTHON_BIN at the top of this file.
    goto :error
)

call :log Using Python: "%PY%"
"%PY%" -I "%ROOT%\tools\ensure_deps.py" --log "%SETUP_LOG%"
if errorlevel 1 goto :error
if /i "%~1"=="setup" ( call :log Setup complete. & goto :eof )

cd /d "%ROOT%"
if /i "%~1"=="debug" (
    "%PY%" -I "%ROOT%\advanced_ctrl_f.py"
    pause
    goto :eof
)
rem pythonw.exe runs without a console window
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" -I "%ROOT%\advanced_ctrl_f.py" %*
goto :eof


rem =====================================================================
:locate_python
if not defined PY call :find_python
if not defined PY call :extract_archives
if not defined PY call :find_python
if not defined PY call :download_python
if not defined PY call :find_python
if not defined PY call :system_venv
if not defined PY call :find_python
goto :eof

:check_python
rem Make sure the interpreter can actually run (Windows may block unsigned programs)
"%PY%" -I -c "import sys" >nul 2>&1
if not errorlevel 1 goto :eof
call :log "%PY%" cannot be started - probably blocked by Windows security.
if /i not "%PY%"=="%PY_DIR%\python\python.exe" (
    set "PY="
    goto :eof
)
call :log Replacing it with the official, signed Python ...
if exist "%PY_DIR%\_blocked" rd /s /q "%PY_DIR%\_blocked"
move "%PY_DIR%\python" "%PY_DIR%\_blocked" >nul
set "PY="
call :download_python
call :find_python
if not defined PY goto :eof
"%PY%" -I -c "import sys" >nul 2>&1
if errorlevel 1 set "PY="
goto :eof

:find_python
set "PY="
if exist "%PY_DIR%\python.exe" ( set "PY=%PY_DIR%\python.exe" & goto :eof )
if exist "%PY_DIR%\python\python.exe" ( set "PY=%PY_DIR%\python\python.exe" & goto :eof )
if exist "%PY_DIR%\venv\Scripts\python.exe" ( set "PY=%PY_DIR%\venv\Scripts\python.exe" & goto :eof )
for /f "delims=" %%F in ('dir /b /s /a-d "%PY_DIR%\python.exe" 2^>nul ^| findstr /v /i "\\Lib\\venv\\ \\_archive\\ \\_blocked\\ \\_download\\"') do (
    if not defined PY set "PY=%%F"
)
goto :eof

:extract_archives
for %%A in ("%PY_DIR%\*.zip" "%PY_DIR%\*.tar.gz" "%PY_DIR%\*.tgz" "%PY_DIR%\*.tar.xz") do (
    if exist "%%~fA" (
        call :log Extracting %%~nxA ...
        tar.exe -xf "%%~fA" -C "%PY_DIR%" || powershell -NoProfile -Command "Expand-Archive -LiteralPath '%%~fA' -DestinationPath '%PY_DIR%' -Force"
        if not exist "%PY_DIR%\_archive" mkdir "%PY_DIR%\_archive"
        move /y "%%~fA" "%PY_DIR%\_archive\" >nul
    )
)
for %%A in ("%PY_DIR%\python-*.exe") do (
    if exist "%%~fA" if /i not "%%~nxA"=="python.exe" (
        call :log Installing %%~nxA into "%PY_DIR%\installed" ...
        "%%~fA" /quiet InstallAllUsers=0 TargetDir="%PY_DIR%\installed" Include_launcher=0 Include_test=0 PrependPath=0 Shortcuts=0 AssociateFiles=0
        if not exist "%PY_DIR%\_archive" mkdir "%PY_DIR%\_archive"
        move /y "%%~fA" "%PY_DIR%\_archive\" >nul
    )
)
goto :eof

:download_python
call :log No local Python found - downloading the latest one ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\tools\get_python.ps1" -Dest "%PY_DIR%" -Version "%PY_VERSION%" -Log "%SETUP_LOG%"
if errorlevel 1 call :log Download failed.
goto :eof

:system_venv
set "SYSPY="
where py >nul 2>&1 && set "SYSPY=py -3"
if not defined SYSPY ( where python >nul 2>&1 && set "SYSPY=python" )
if not defined SYSPY goto :eof
call :log Using system Python in a dedicated venv ...
%SYSPY% -m venv "%PY_DIR%\venv"
goto :eof

:log
rem echo a message and append it to data\setup.log
echo [Advanced Ctrl+F] %*
>>"%SETUP_LOG%" echo [Advanced Ctrl+F] %*
goto :eof

:error
echo.
call :log Could not start. See the messages above and data\setup.log.
pause
exit /b 1
