@echo off
REM Double-click this when "Start Calculator (Windows).bat" opens a window that
REM closes again before anything can be read. It runs the launcher as a CHILD
REM process, so even a launcher that is killed outright still leaves this
REM window standing and a report on disk.
cd /d "%~dp0"

if not exist "%~dp0tools\diagnose.ps1" goto missing

echo.
echo   Collecting diagnostics for the Position Size Calculator.
echo   This starts the app once and writes an HTML report; it can take a few
echo   minutes if Python still has to be downloaded. Leave this window open.
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\diagnose.ps1"
set "STATUS=%errorlevel%"
if "%STATUS%"=="0" goto done

echo.
echo   The diagnostics could not run -- exit code %STATUS%.
echo   Exit code 9009 means Windows could not find PowerShell.
goto done

:missing
echo.
echo   tools\diagnose.ps1 is missing from this folder, so the ZIP was not
echo   fully extracted. Right-click the ZIP, choose "Extract All", and start
echo   again from the extracted folder.
set "STATUS=1"

:done
echo.
pause
exit /b %STATUS%
