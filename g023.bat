@echo off
REM g023 Code launcher (Windows)
REM Usage from any project folder:
REM   C:\path\to\g023-code\g023.bat
REM   or after adding to PATH: g023

setlocal

set "SCRIPT_DIR=%~dp0"
set "G023_HOME=%SCRIPT_DIR%"
set "G023_PROJECT_ROOT=%CD%"

set "PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%"

REM A venv created by installer.bat wins over whatever python is on PATH: it is
REM the one interpreter we know has the dependencies.
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    "%SCRIPT_DIR%.venv\Scripts\python.exe" -m g023_code %*
    goto :done
)

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python -m g023_code %*
    goto :done
)
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py -3 -m g023_code %*
    goto :done
)
echo Error: Python 3 is required but not found. Run installer.bat first.
exit /b 1

:done

endlocal
