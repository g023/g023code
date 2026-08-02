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

where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
    python -m g023_code %*
) else (
    where py >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        py -3 -m g023_code %*
    ) else (
        echo Error: Python 3 is required but not found.
        exit /b 1
    )
)

endlocal
