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
rem  A newer Python that the program prepared in the background (python\_next\,
rem  verified and with all packages) replaces the downloaded one at the next
rem  start; the old one is deleted.
rem
rem  Usage:  start.cmd           start normally (no console window)
rem          start.cmd debug     with console (errors are visible)
rem          start.cmd setup     set up only
rem =====================================================================
setlocal EnableExtensions DisableDelayedExpansion
rem Programs are never looked up in the current folder, and this launcher works in its own
rem folder from the first line on; system tools are called with their full path.
set "NoDefaultCurrentDirectoryInExePath=1"
cd /d "%~dp0"
set "SYS32=%SystemRoot%\System32"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

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
"%SYS32%\chcp.com" 65001 >nul
rem Messages go through the variable LOGMSG (see :log): file and folder names are data,
rem never part of a command line (a folder named "a & calc" must not run anything).
set "LOGMSG====== %DATE% %TIME% start.cmd %~1 | %PROCESSOR_ARCHITECTURE% | %ROOT%"
call :log_file
>>"%SETUP_LOG%" ver

rem ---- An update that was interrupted while it replaced the program files is rolled back.
rem      All of it runs inside one block: cmd reads a block completely before running it, so
rem      replacing this very file cannot confuse it. Then the restored launcher is started.
set "UPD=%ROOT%\data\updates"
set "BK="
if exist "%UPD%\applying" set /p BK=<"%UPD%\applying"
if not defined BK set "BK=-"
if not "%BK:~0,7%"=="backup-" set "BK=-"
set "ACF_RB_FAIL="
set "ACF_SELF=%~f0"
rem In the block: "%%T"=="%%~nxT" holds only for a plain name (no \ / : .. or drive), and
rem CALL "%%ACF_SELF%%" lets CALL expand the variable - the path itself is never expanded twice.
if exist "%UPD%\applying" (
    echo [Advanced Ctrl+F] An interrupted update is rolled back ...
    >>"%SETUP_LOG%" echo [Advanced Ctrl+F] An interrupted update is rolled back ...
    for /f "usebackq skip=1 tokens=1,2" %%S in ("%UPD%\applying") do if "%%T"=="%%~nxT" (
        if /i not "%%T"=="data" if /i not "%%T"=="config" if /i not "%%T"=="python" if /i not "%%T"==".git" (
            if /i "%%S"=="new" (
                if exist "%ROOT%\%%T\" rd /s /q "%ROOT%\%%T" 2>nul
                if exist "%ROOT%\%%T" if not exist "%ROOT%\%%T\" del /f /q "%ROOT%\%%T" 2>nul
                if exist "%ROOT%\%%T" set "ACF_RB_FAIL=1"
            )
            if /i "%%S"=="old" if exist "%UPD%\%BK%\%%T" (
                if exist "%ROOT%\%%T\" rd /s /q "%ROOT%\%%T" 2>nul
                if exist "%UPD%\%BK%\%%T\" if exist "%ROOT%\%%T" if not exist "%ROOT%\%%T\" del /f /q "%ROOT%\%%T" 2>nul
                if exist "%UPD%\%BK%\%%T\" (
                    if exist "%ROOT%\%%T" ( set "ACF_RB_FAIL=1" ) else ( move "%UPD%\%BK%\%%T" "%ROOT%\%%T" >nul || set "ACF_RB_FAIL=1" )
                ) else (
                    if exist "%ROOT%\%%T\" ( set "ACF_RB_FAIL=1" ) else ( move /y "%UPD%\%BK%\%%T" "%ROOT%\%%T" >nul || set "ACF_RB_FAIL=1" )
                )
            )
        )
    )
    if defined ACF_RB_FAIL (
        echo [Advanced Ctrl+F] Rolling back the interrupted update failed - close programs using this folder and start again.
        >>"%SETUP_LOG%" echo [Advanced Ctrl+F] Rolling back the interrupted update failed.
        if not defined CI pause
        exit /b 1
    )
    del /f /q "%UPD%\applying"
    call "%%ACF_SELF%%" %*
    exit /b
)
if not exist "%PY_DIR%" mkdir "%PY_DIR%"
set "PY="

if defined PYTHON_BIN (
    if exist "%PYTHON_BIN%" ( set "PY=%PYTHON_BIN%" ) else (
        set "LOGMSG=PYTHON_BIN points to a file that does not exist: %PYTHON_BIN%" & call :log
        goto :error
    )
)
if not defined PYTHON_BIN call :switch_python
call :locate_python
if defined PY call :check_python
if not defined PY (
    set "LOGMSG=No Python found. Put a Python or an archive into %PY_DIR%" & call :log
    set "LOGMSG=or set PYTHON_BIN at the top of this file." & call :log
    goto :error
)

set "LOGMSG=Using Python: %PY%" & call :log
"%PY%" -I "%ROOT%\tools\ensure_deps.py" --log "%SETUP_LOG%"
if errorlevel 1 goto :error
if /i "%~1"=="setup" ( set "LOGMSG=Setup complete." & call :log & goto :eof )

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
:switch_python
rem python\_next\ holds a newer, verified Python with all packages (prepared by the program)
rem A switch that was interrupted between its two moves left only _old: put it back first.
if exist "%PY_DIR%\_old\" if not exist "%PY_DIR%\python\" (
    move "%PY_DIR%\_old" "%PY_DIR%\python" >nul 2>&1
    if exist "%PY_DIR%\python\" ( set "LOGMSG=Restored the previous Python (a switch had been interrupted)." & call :log )
)
rem _old is deleted only when the current Python is in place
if exist "%PY_DIR%\_old" if exist "%PY_DIR%\python\" rd /s /q "%PY_DIR%\_old" 2>nul
if not exist "%PY_DIR%\_next\READY" goto :eof
if not exist "%PY_DIR%\python\python.exe" goto :eof
"%PY_DIR%\_next\python\python.exe" -I -c "import sys" >nul 2>&1
if errorlevel 1 (
    set "LOGMSG=Prepared Python does not start - discarded." & call :log
    rd /s /q "%PY_DIR%\_next" 2>nul
    goto :eof
)
set /p NEXT_VER=<"%PY_DIR%\_next\READY"
set "LOGMSG=Switching to Python %NEXT_VER% ..." & call :log
move "%PY_DIR%\python" "%PY_DIR%\_old" >nul 2>&1
if errorlevel 1 ( set "LOGMSG=The current Python is still in use - switching at the next start." & call :log & goto :eof )
move "%PY_DIR%\_next\python" "%PY_DIR%\python" >nul 2>&1
if errorlevel 1 (
    move "%PY_DIR%\_old" "%PY_DIR%\python" >nul 2>&1
    set "LOGMSG=Switching failed - keeping the current Python." & call :log
    goto :eof
)
rd /s /q "%PY_DIR%\_next" 2>nul
rd /s /q "%PY_DIR%\_old" 2>nul
set "LOGMSG=Old Python removed." & call :log
goto :eof

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
set "LOGMSG=%PY% cannot be started - probably blocked by Windows security." & call :log
if /i not "%PY%"=="%PY_DIR%\python\python.exe" (
    set "PY="
    goto :eof
)
set "LOGMSG=Replacing it with the official, signed Python ..." & call :log
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
for /f "delims=" %%F in ('dir /b /s /a-d "%PY_DIR%\python.exe" 2^>nul ^| "%SYS32%\findstr.exe" /v /i "\\Lib\\venv\\ \\_archive\\ \\_blocked\\ \\_download\\ \\_next\\ \\_next.tmp\\ \\_old\\"') do (
    if not defined PY set "PY=%%F"
)
goto :eof

:extract_archives
for %%A in ("%PY_DIR%\*.zip" "%PY_DIR%\*.tar.gz" "%PY_DIR%\*.tgz" "%PY_DIR%\*.tar.xz") do (
    if exist "%%~fA" (
        set "LOGMSG=Extracting %%~nxA ..." & call :log
        set "ACF_FILE=%%~fA"
        set "ACF_DEST=%PY_DIR%"
        "%SYS32%\tar.exe" -xf "%%~fA" -C "%PY_DIR%" || "%PS%" -NoProfile -NonInteractive -Command "Expand-Archive -LiteralPath $env:ACF_FILE -DestinationPath $env:ACF_DEST -Force"
        if not exist "%PY_DIR%\_archive" mkdir "%PY_DIR%\_archive"
        move /y "%%~fA" "%PY_DIR%\_archive\" >nul
    )
)
rem an installer is only run if it carries a valid signature of the Python Software Foundation
rem (the path reaches PowerShell through an environment variable, never as command text)
for %%A in ("%PY_DIR%\python-*.exe") do (
    if exist "%%~fA" if /i not "%%~nxA"=="python.exe" (
        set "ACF_FILE=%%~fA"
        "%PS%" -NoProfile -NonInteractive -Command "$s = Get-AuthenticodeSignature -LiteralPath $env:ACF_FILE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') { exit 1 }"
        if errorlevel 1 (
            set "LOGMSG=%%~nxA is not validly signed by the Python Software Foundation - not installed, moved to _blocked" & call :log
            if not exist "%PY_DIR%\_blocked" mkdir "%PY_DIR%\_blocked"
            move /y "%%~fA" "%PY_DIR%\_blocked\" >nul
        ) else (
            set "LOGMSG=Installing %%~nxA into %PY_DIR%\installed ..." & call :log
            "%%~fA" /quiet InstallAllUsers=0 TargetDir="%PY_DIR%\installed" Include_launcher=0 Include_test=0 PrependPath=0 Shortcuts=0 AssociateFiles=0
            if not exist "%PY_DIR%\_archive" mkdir "%PY_DIR%\_archive"
            move /y "%%~fA" "%PY_DIR%\_archive\" >nul
        )
    )
)
goto :eof

:download_python
set "LOGMSG=No local Python found - downloading the latest one ..." & call :log
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\tools\get_python.ps1" -Dest "%PY_DIR%" -Version "%PY_VERSION%" -Log "%SETUP_LOG%"
if errorlevel 1 ( set "LOGMSG=Download failed." & call :log )
goto :eof

:system_venv
set "SYSPY="
"%SYS32%\where.exe" py >nul 2>&1 && set "SYSPY=py -3"
if not defined SYSPY ( "%SYS32%\where.exe" python >nul 2>&1 && set "SYSPY=python" )
if not defined SYSPY goto :eof
set "LOGMSG=Using system Python in a dedicated venv ..." & call :log
%SYSPY% -m venv "%PY_DIR%\venv"
goto :eof

:log
rem shows the message in LOGMSG and appends it to data\setup.log. Delayed expansion (!LOGMSG!)
rem inserts the text after the command line has been read, so & | < > ( ) %% in names stay text.
setlocal EnableDelayedExpansion
echo([Advanced Ctrl+F] !LOGMSG!
>>"!SETUP_LOG!" echo([Advanced Ctrl+F] !LOGMSG!
endlocal
goto :eof

:log_file
setlocal EnableDelayedExpansion
>>"!SETUP_LOG!" echo(!LOGMSG!
endlocal
goto :eof

:error
echo.
set "LOGMSG=Could not start. See the messages above and data\setup.log." & call :log
if not defined CI pause
exit /b 1
