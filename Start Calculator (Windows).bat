@echo off
REM Double-click launcher for Windows. Hands straight off to run.py, which does
REM the real work. %~dp0 is this file's own folder, so the app works wherever it
REM is unzipped -- including under a path with spaces.
cd /d "%~dp0"

REM Prefer the "py" launcher: it is what the python.org installer registers, and
REM it works even when python.exe was left off PATH (the most common cause of
REM "python is not recognized"). Plain "python" is the fallback.
REM Structured with goto rather than if/else blocks, because %errorlevel% inside
REM a parenthesised block is expanded before the block runs.
where py >nul 2>nul
if %errorlevel%==0 goto use_py

where python >nul 2>nul
if %errorlevel%==0 goto use_python

echo Python 3 is not installed on this PC.
echo.
echo Install it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" on the first installer screen,
echo then double-click this file again.
goto done

:use_py
py -3 run.py
goto done

:use_python
python run.py
goto done

:done
echo.
pause
