@echo off
REM Double-click launcher for Windows. Hands off to run.py, which installs the
REM Python libraries and starts the app; this file's own job is to make sure a
REM Python 3.10+ exists to run it with, downloading a private one if the PC has
REM none. %~dp0 is this file's own folder, so the app works wherever it is
REM unzipped -- including under a path with spaces.
cd /d "%~dp0"

set "PY_VERSION=3.12.14"
set "PY_BUILD=20260901"
set "PY_TRIPLE=x86_64-pc-windows-msvc"
set "PY_SHA256=e90c1b6419da3bd812dd73bb3de40287a21abf153438147639ec5e20375ea93f"
set "RUNTIME_DIR=%~dp0.runtime"
set "RUNTIME_PY=%RUNTIME_DIR%\python\python.exe"
set "STAGING=%RUNTIME_DIR%\.download"
set "PY_ARCHIVE=cpython-%PY_VERSION%+%PY_BUILD%-%PY_TRIPLE%-install_only.tar.gz"
set "PY_URL=https://github.com/astral-sh/python-build-standalone/releases/download/%PY_BUILD%/%PY_ARCHIVE%"
set "VERCHECK=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
REM Defaults to failure, so a path that somehow reaches :done without setting
REM it reports a problem rather than a silent success.
set "RUN_STATUS=1"

REM A copy downloaded on an earlier run wins over the system Python: run.py's
REM .venv is bound to whichever interpreter created it, so quietly switching
REM interpreters between runs would leave that .venv unusable.
if not exist "%RUNTIME_PY%" goto try_py
"%RUNTIME_PY%" -c "%VERCHECK%" >nul 2>nul
if %errorlevel%==0 goto use_runtime

REM Prefer the "py" launcher: it is what the python.org installer registers, and
REM it works even when python.exe was left off PATH (the most common cause of
REM "python is not recognized"). Plain "python" is the fallback. 3.10 is run.py's
REM floor, so an installed-but-too-old Python must fall through to the download.
:try_py
where py >nul 2>nul
if not %errorlevel%==0 goto try_python
py -3 -c "%VERCHECK%" >nul 2>nul
if %errorlevel%==0 goto use_py

:try_python
where python >nul 2>nul
if not %errorlevel%==0 goto provision
python -c "%VERCHECK%" >nul 2>nul
if %errorlevel%==0 goto use_python

:provision
where tar >nul 2>nul
if not %errorlevel%==0 goto no_tar

echo This PC does not have Python, so the app will download its own copy
echo (about 45 MB). It goes in this folder only -- nothing is installed
echo system-wide, and no administrator password is needed.
echo.

if exist "%STAGING%" rd /s /q "%STAGING%"
mkdir "%STAGING%" 2>nul
if not exist "%STAGING%" goto download_failed

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%PY_URL%' -OutFile '%STAGING%\%PY_ARCHIVE%' -UseBasicParsing } catch { exit 1 }"
if not %errorlevel%==0 goto download_failed
if not exist "%STAGING%\%PY_ARCHIVE%" goto download_failed

powershell -NoProfile -ExecutionPolicy Bypass -Command "$h = (Get-FileHash -Algorithm SHA256 '%STAGING%\%PY_ARCHIVE%').Hash.ToLower(); if ($h -ne '%PY_SHA256%') { Write-Host ('  expected: %PY_SHA256%'); Write-Host ('  received: ' + $h); exit 1 }"
if not %errorlevel%==0 goto checksum_failed

tar -xf "%STAGING%\%PY_ARCHIVE%" -C "%STAGING%"
if not %errorlevel%==0 goto unpack_failed

if exist "%RUNTIME_DIR%\python" rd /s /q "%RUNTIME_DIR%\python"
move "%STAGING%\python" "%RUNTIME_DIR%\python" >nul
if not exist "%RUNTIME_PY%" goto unpack_failed
rd /s /q "%STAGING%"

"%RUNTIME_PY%" -c "%VERCHECK%" >nul 2>nul
if not %errorlevel%==0 goto unpack_failed
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
%PY_CMD% run.py
set "RUN_STATUS=%errorlevel%"
if "%RUN_STATUS%"=="0" goto done
REM Without this the window would close on an unexplained failure, and the
REM launcher would report success to whatever started it.
echo.
echo Something went wrong -- exit code %RUN_STATUS%.
goto done

:no_tar
echo This version of Windows is too old to unpack the Python download
echo automatically (it has no "tar" command).
goto manual

:download_failed
echo The download failed. Check your internet connection and try again.
goto manual

:checksum_failed
echo The downloaded Python does not match its expected checksum, so it will
echo not be used.
goto manual

:unpack_failed
echo The Python download could not be unpacked.
goto manual

:manual
echo.
echo The README's "Installing Python" section walks through installing Python
echo by hand, after which this file will work.
set "RUN_STATUS=1"
goto done

:done
echo.
pause
exit /b %RUN_STATUS%
