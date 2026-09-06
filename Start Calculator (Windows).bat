@echo off
REM Double-click launcher for Windows. Hands off to run.py, which installs the
REM Python libraries and starts the app; this file's own job is to make sure a
REM Python 3.10+ exists to run it with, downloading a private one if the PC has
REM none. %~dp0 is this file's own folder, so the app works wherever it is
REM unzipped -- including under a path with spaces.
cd /d "%~dp0"

REM Every step below is appended to a log as it happens, not at the end: if the
REM window vanishes mid-run the log still shows the last step that completed,
REM which is the only evidence left once the console is gone.
set "LOG_DIR=%~dp0logs"
set "LOG=%LOG_DIR%\launcher-steps.log"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
REM A read-only folder must not make every later line fail loudly -- fall back
REM to somewhere always writable rather than throwing the evidence away.
if not exist "%LOG_DIR%" set "LOG=%TEMP%\psc-launcher-steps.log"
>"%LOG%" echo === Position Size Calculator launcher === %DATE% %TIME%
>>"%LOG%" echo script folder : %~dp0
>>"%LOG%" echo working dir   : %CD%
>>"%LOG%" echo command shell : %ComSpec%

REM Two different failures, told apart by testing the same file twice: absent
REM under its full path means the ZIP was never properly extracted; present
REM there but not reachable relatively means the cd above did not take effect,
REM which is what happens on a UNC path since cd /d cannot enter one.
if not exist "%~dp0run.py" goto missing_files
if not exist "%~dp0app.py" goto missing_files
if not exist "%~dp0requirements.txt" goto missing_files
if not exist "run.py" goto wrong_directory

set "PY_VERSION=3.12.14"
set "PY_BUILD=20260901"
set "PY_TRIPLE=x86_64-pc-windows-msvc"
set "PY_SHA256=e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f"
set "RUNTIME_DIR=%~dp0.runtime"
set "RUNTIME_PY=%RUNTIME_DIR%\python\python.exe"
set "STAGING=%RUNTIME_DIR%\.download"
set "PY_ARCHIVE=cpython-%PY_VERSION%+%PY_BUILD%-%PY_TRIPLE%-install_only.tar.gz"
set "PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/%PY_BUILD%/%PY_ARCHIVE%"
REM The 3.14 ceiling is not arbitrary and must not be raised on its own:
REM tradingview-mcp-server==0.8.1 declares Requires-Python >=3.10,<3.14, so on a
REM newer Python this check would pass, a venv would be built, and pip would
REM only then refuse to install -- long after the interpreter was chosen.
REM Failing the probe instead sends a too-new PC down the download path to the
REM private 3.12, which is exactly what that path exists for.
set "VERCHECK=import sys; sys.exit(0 if (3, 10) <= sys.version_info < (3, 14) else 1)"
REM Defaults to failure, so a path that somehow reaches :done without setting
REM it reports a problem rather than a silent success.
set "RUN_STATUS=1"

REM A copy downloaded on an earlier run wins over the system Python: run.py's
REM .venv is bound to whichever interpreter created it, so quietly switching
REM interpreters between runs would leave that .venv unusable.
if not exist "%RUNTIME_PY%" goto try_py
"%RUNTIME_PY%" -c "%VERCHECK%" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "downloaded runtime version check -> exit %EL%"
if "%EL%"=="0" goto use_runtime

REM Prefer the "py" launcher: it is what the python.org installer registers, and
REM it works even when python.exe was left off PATH (the most common cause of
REM "python is not recognized"). Plain "python" is the fallback. 3.10 is run.py's
REM floor, so an installed-but-too-old Python must fall through to the download.
:try_py
where py >nul 2>nul
set "EL=%errorlevel%"
call :log "where py -> exit %EL%"
if not "%EL%"=="0" goto try_python
py -3 -c "%VERCHECK%" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "py -3 version check -> exit %EL%"
if "%EL%"=="0" goto use_py

:try_python
where python >nul 2>nul
set "EL=%errorlevel%"
call :log "where python -> exit %EL%"
if not "%EL%"=="0" goto provision
python -c "%VERCHECK%" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "python version check -> exit %EL%"
if "%EL%"=="0" goto use_python

:provision
call :log "no usable Python found -- provisioning a private copy"
where tar >nul 2>nul
set "EL=%errorlevel%"
call :log "where tar -> exit %EL%"
if not "%EL%"=="0" goto no_tar

echo This PC does not have Python, so the app will download its own copy
echo (about 45 MB). It goes in this folder only -- nothing is installed
echo system-wide, and no administrator password is needed.
echo.

if exist "%STAGING%" rd /s /q "%STAGING%"
mkdir "%STAGING%" 2>nul
if not exist "%STAGING%" goto download_failed

call :log "downloading %PY_URL%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%STAGING%\%PY_ARCHIVE%' -UseBasicParsing } catch { Write-Host $_.Exception.Message; exit 1 }" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "download -> exit %EL%"
if not "%EL%"=="0" goto download_failed
if not exist "%STAGING%\%PY_ARCHIVE%" goto download_failed

powershell -NoProfile -ExecutionPolicy Bypass -Command "$h = (Get-FileHash -Algorithm SHA256 '%STAGING%\%PY_ARCHIVE%').Hash.ToLower(); if ($h -ne '%PY_SHA256%') { Write-Host ('  expected: %PY_SHA256%'); Write-Host ('  received: ' + $h); exit 1 }"
set "EL=%errorlevel%"
call :log "checksum -> exit %EL%"
if not "%EL%"=="0" goto checksum_failed

tar -xf "%STAGING%\%PY_ARCHIVE%" -C "%STAGING%" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "tar -xf -> exit %EL%"
if not "%EL%"=="0" goto unpack_failed

if exist "%RUNTIME_DIR%\python" rd /s /q "%RUNTIME_DIR%\python"
move "%STAGING%\python" "%RUNTIME_DIR%\python" >nul
if not exist "%RUNTIME_PY%" goto unpack_failed
rd /s /q "%STAGING%"

"%RUNTIME_PY%" -c "%VERCHECK%" >>"%LOG%" 2>&1
set "EL=%errorlevel%"
call :log "new runtime version check -> exit %EL%"
if not "%EL%"=="0" goto unpack_failed
goto use_runtime

:use_runtime
set PY_CMD="%RUNTIME_PY%"
goto run

:use_py
set PY_CMD=py -3
goto run

:use_python
set PY_CMD=python
goto run

:run
REM Echoed directly rather than through :log -- PY_CMD is itself quoted when it
REM holds a path, and nested quotes truncate a "call" argument.
>>"%LOG%" echo [%TIME%] starting run.py with %PY_CMD%
%PY_CMD% run.py
set "RUN_STATUS=%errorlevel%"
call :log "run.py -> exit %RUN_STATUS%"
if "%RUN_STATUS%"=="0" goto done
REM Without this the window would close on an unexplained failure, and the
REM launcher would report success to whatever started it.
echo.
echo Something went wrong -- exit code %RUN_STATUS%.
echo A log of every step is in: %LOG%
echo For a full report, double-click "Diagnose (Windows).bat".
goto done

:wrong_directory
echo.
echo This file could not switch to its own folder. That happens when the app
echo is run straight from a network location instead of a folder on this PC.
echo Copy the whole folder to your Desktop or Documents and start it there.
call :log "cd failed: expected %~dp0 but working dir is %CD%"
goto manual

:missing_files
echo.
echo Some of the app's files are missing from this folder. This normally means
echo the ZIP was not fully extracted -- double-clicking a ZIP on Windows only
echo previews it. Right-click the ZIP, choose "Extract All", then start the
echo app from the extracted folder.
call :log "missing files in %~dp0"
goto manual

:no_tar
echo This version of Windows is too old to unpack the Python download
echo automatically (it has no "tar" command).
call :log "tar is not available"
goto manual

:download_failed
echo The download failed. Check your internet connection and try again.
call :log "download failed"
goto manual

:checksum_failed
echo The downloaded Python does not match its expected checksum, so it will
echo not be used.
call :log "checksum mismatch"
goto manual

:unpack_failed
echo The Python download could not be unpacked.
call :log "unpack failed"
goto manual

:manual
echo.
echo The README's "Installing Python" section walks through installing Python
echo by hand, after which this file will work.
set "RUN_STATUS=1"
goto done

:done
call :log "finished with status %RUN_STATUS%"
echo.
pause
exit /b %RUN_STATUS%

REM Placed past the exit above so normal flow can never fall into it.
:log
>>"%LOG%" echo [%TIME%] %~1
goto :eof
