@echo off
setlocal EnableDelayedExpansion
REM ===========================================================================
REM  g023 Code - setup for Windows
REM
REM  Drop the project in a folder, double-click this file (or run it from a
REM  terminal), and it brings the machine up to a working install. Every step
REM  checks what is already true and does only the part that is missing, so
REM  re-running it is both safe and the normal way to repair a half-finished
REM  setup.
REM
REM    installer.bat                  interactive, recommended
REM    installer.bat /y               accept every default, no questions
REM    installer.bat /key sk-...      supply the API key non-interactively
REM    installer.bat /nopath          do not touch the user PATH
REM    installer.bat /uninstall       undo the PATH entry
REM
REM  Nothing outside this folder is changed except the user PATH entry, and
REM  only with your agreement.
REM ===========================================================================

set "HERE=%~dp0"
if "%HERE:~-1%"=="\" set "HERE=%HERE:~0,-1%"
set "VENV=%HERE%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "STEP=0"
set "WARNCOUNT=0"

set "ASSUME_YES=0"
set "DO_PATH=1"
set "DO_OPTIONAL=auto"
set "API_KEY="
set "UNINSTALL=0"

:parse
if "%~1"=="" goto parsed
if /I "%~1"=="/y"            ( set "ASSUME_YES=1" & shift & goto parse )
if /I "%~1"=="/yes"          ( set "ASSUME_YES=1" & shift & goto parse )
if /I "%~1"=="/nopath"       ( set "DO_PATH=0"    & shift & goto parse )
if /I "%~1"=="/optional"     ( set "DO_OPTIONAL=yes" & shift & goto parse )
if /I "%~1"=="/nooptional"   ( set "DO_OPTIONAL=no"  & shift & goto parse )
if /I "%~1"=="/uninstall"    ( set "UNINSTALL=1"  & shift & goto parse )
if /I "%~1"=="/key"          ( set "API_KEY=%~2"  & shift & shift & goto parse )
if /I "%~1"=="/?"            goto usage
if /I "%~1"=="/h"            goto usage
if /I "%~1"=="--help"        goto usage
echo Unknown option: %~1
goto usage
:parsed

if "%UNINSTALL%"=="1" goto uninstall

echo.
echo   g023 Code - setup
echo   %HERE%

REM ---------------------------------------------------------------------------
call :step "Checking the project layout"
REM ---------------------------------------------------------------------------
if not exist "%HERE%\g023_code\__init__.py" goto no_package
if not exist "%HERE%\g023_code\cli.py"      goto no_package
if not exist "%HERE%\requirements.txt"      goto no_package
call :ok "package, launcher and requirements are all here"

REM ---------------------------------------------------------------------------
call :step "Looking for Python 3.11 or newer"
REM ---------------------------------------------------------------------------
REM The py launcher knows about every installed version, so ask it first and let
REM it pick the newest 3.x; fall back to whatever "python" resolves to. Note the
REM Microsoft Store stub also answers to "python" and does nothing useful, which
REM the version probe below catches.
set "PYTHON="
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON=%%P"
)
if not defined PYTHON (
    for /f "delims=" %%P in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PYTHON=%%P"
)
if not defined PYTHON goto no_python

"%PYTHON%" -c "import sys;sys.exit(0 if sys.version_info[:2]>=(3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    call :fail "found %PYTHON% but it is older than 3.11"
    goto no_python
)
for /f "delims=" %%V in ('"%PYTHON%" -c "import sys;print('%%d.%%d.%%d'%%sys.version_info[:3])"') do set "PYVER=%%V"
call :ok "%PYTHON% (%PYVER%)"

REM ---------------------------------------------------------------------------
call :step "Setting up the virtual environment"
REM ---------------------------------------------------------------------------
REM A venv in the project folder keeps g023's dependencies away from the system
REM interpreter, and g023.bat prefers it automatically.
if exist "%VPY%" (
    call :skip "reusing the existing .venv"
) else (
    echo     creating .venv ...
    "%PYTHON%" -m venv "%VENV%"
    if errorlevel 1 (
        call :fail "could not create the virtual environment"
        echo     Repair your Python install ^(tick 'pip' and 'venv' in the installer^) and try again.
        goto abort
    )
    call :ok "created .venv"
)
if not exist "%VPY%" ( call :fail "the venv is missing its python.exe" & goto abort )
set "PYTHON=%VPY%"
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 "%PYTHON%" -m ensurepip --upgrade >nul 2>&1
"%PYTHON%" -m pip --version >nul 2>&1
if errorlevel 1 ( call :fail "pip is not available inside the venv" & goto abort )

REM ---------------------------------------------------------------------------
call :step "Installing dependencies"
REM ---------------------------------------------------------------------------
call :need rich        "rich>=13.7.0"        required
call :need httpx       "httpx>=0.27.0"       required
call :need tiktoken    "tiktoken>=0.7.0"     required
call :need pygments    "pygments>=2.17.0"    required
if defined FATAL goto abort

call :need prompt_toolkit "prompt_toolkit>=3.0.0" recommended

REM Optional extras: vision preprocessing and browser-grade fetching. Asked for
REM as a group, installed one at a time so a package with no wheel for this
REM Python cannot take the rest down with it.
set "MISSING_OPTIONAL="
call :probe PIL         "pillow>=10.0.0"
call :probe curl_cffi   "curl_cffi>=0.7.0"
call :probe h2          "h2>=4.1.0"
call :probe brotli      "brotli>=1.1.0"
call :probe zstandard   "zstandard>=0.22.0"

if defined MISSING_OPTIONAL (
    set "WANT_OPT=0"
    if /I "%DO_OPTIONAL%"=="yes" set "WANT_OPT=1"
    if /I "%DO_OPTIONAL%"=="auto" (
        call :ask "Install the optional extras (vision + browser-grade fetching)?" y
        if "!ANSWER!"=="y" set "WANT_OPT=1"
    )
    if "!WANT_OPT!"=="1" (
        REM %%S keeps its quotes on purpose: a spec like pillow>=10.0.0 is
        REM expanded before cmd looks for redirection, so an unquoted >= would
        REM be read as "redirect into a file called =10.0.0".
        for %%S in (!MISSING_OPTIONAL!) do (
            echo     pip install %%S ...
            "%PYTHON%" -m pip install --disable-pip-version-check --quiet %%S
            if errorlevel 1 ( call :warn "%%~S could not be installed - g023 runs without it" ) else ( call :ok "installed %%~S" )
        )
    ) else (
        call :skip "skipping optional extras (add them later: .venv\Scripts\pip install -r requirements.txt)"
    )
)

REM ---------------------------------------------------------------------------
call :step "API key"
REM ---------------------------------------------------------------------------
set "KEYFILE=%HERE%\K.dat"
set "CURKEY="
if exist "%KEYFILE%" for /f "usebackq delims=" %%K in ("%KEYFILE%") do if not defined CURKEY set "CURKEY=%%K"

set "KEYOK=0"
if defined CURKEY (
    set "KEYOK=1"
    if /I "!CURKEY!"=="YOUR_DEEPSEEK_API_KEY_HERE" set "KEYOK=0"
    if /I "!CURKEY!"=="sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY" set "KEYOK=0"
)

if defined API_KEY (
    > "%KEYFILE%" echo !API_KEY!
    call :ok "wrote the key you passed into K.dat"
) else if "!KEYOK!"=="1" (
    call :ok "K.dat already holds a key"
) else if defined DEEPSEEK_API_KEY (
    > "%KEYFILE%" echo %DEEPSEEK_API_KEY%
    call :ok "took the key from the DEEPSEEK_API_KEY environment variable"
) else if "%ASSUME_YES%"=="1" (
    REM The redirect lives inside its own block so cmd cannot bind it to the
    REM enclosing 'if' and truncate an existing K.dat on the false branch.
    if not exist "%KEYFILE%" (
        > "%KEYFILE%" echo sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY
    )
    call :warn "no key yet - put one on the first line of %KEYFILE%"
    call :addwarn "K.dat has no real key. g023 will not start until you add one (https://platform.deepseek.com/)."
) else (
    echo     Get one at https://platform.deepseek.com/ - leave blank to add it later.
    set "ENTERED="
    set /p "ENTERED=    DeepSeek API key: "
    if defined ENTERED (
        > "%KEYFILE%" echo !ENTERED!
        call :ok "saved to K.dat"
    ) else (
        if not exist "%KEYFILE%" (
            > "%KEYFILE%" echo sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY
        )
        call :warn "left blank - add your key to %KEYFILE% before running g023"
        call :addwarn "K.dat has no real key yet."
    )
)

REM ---------------------------------------------------------------------------
call :step "Configuration"
REM ---------------------------------------------------------------------------
set "CFG=%HERE%\config.json"
if exist "%CFG%" (
    "%PYTHON%" -c "import json,sys;json.load(open(sys.argv[1]))" "%CFG%" >nul 2>&1
    if errorlevel 1 (
        move /y "%CFG%" "%CFG%.broken" >nul
        call :warn "config.json was not valid JSON; kept it as config.json.broken and rewriting defaults"
    ) else (
        call :skip "config.json is present and valid - left untouched"
    )
)
if not exist "%CFG%" (
    > "%CFG%" (
        echo {
        echo   "verbose": "low",
        echo   "auto_compact": true,
        echo   "vision_backend": "none",
        echo   "vision_model": null,
        echo   "vision_host": null,
        echo   "vision_max_image_dim": 1024,
        echo   "vision_timeout": 180,
        echo   "orchestrator_model": "deepseek-v4-flash",
        echo   "subagent_model": "deepseek-v4-flash",
        echo   "reasoning_effort": "high",
        echo   "thinking_enabled": true,
        echo   "show_tool_timing": true,
        echo   "show_context_bar": true,
        echo   "permission_default": "ask",
        echo   "vision_num_ctx": 4096,
        echo   "vision_keep_alive": "5m"
        echo }
    )
    call :ok "wrote default config.json"
)

REM ---------------------------------------------------------------------------
call :step "Vision backend (optional)"
REM ---------------------------------------------------------------------------
REM Vision is off by default and stays off - this only reports what is around,
REM so /vision has something to offer when you want it.
where ollama >nul 2>&1
if %ERRORLEVEL% equ 0 (
    call :ok "ollama found - enable image analysis inside g023 with /vision"
) else (
    call :skip "no ollama on this machine - image analysis stays off (everything else works)"
    call :skip "install it from https://ollama.com if you want /vision"
)

REM ---------------------------------------------------------------------------
call :step "Making 'g023' runnable from anywhere"
REM ---------------------------------------------------------------------------
if "%DO_PATH%"=="0" (
    call :skip "/nopath given; launch with %HERE%\g023.bat"
    goto path_done
)

REM Read PATH from the registry, never from %PATH%: the live variable is the
REM user and machine paths already joined, and writing that back would copy the
REM whole system PATH into the user hive.
set "USERPATH="
for /f "usebackq tokens=2,*" %%A in (`reg query HKCU\Environment /v Path 2^>nul`) do set "USERPATH=%%B"

echo !USERPATH! | find /I "%HERE%" >nul
if %ERRORLEVEL% equ 0 (
    call :ok "this folder is already on your PATH - type g023 in any project folder"
    goto path_done
)

call :ask "Add %HERE% to your user PATH so 'g023' works anywhere?" y
if not "!ANSWER!"=="y" (
    call :skip "left PATH alone; launch with %HERE%\g023.bat"
    call :addwarn "The install folder is not on PATH, so plain 'g023' will not resolve."
    goto path_done
)

if defined USERPATH (
    set "NEWPATH=!USERPATH!;%HERE%"
) else (
    set "NEWPATH=%HERE%"
)
REM Two ways setx can quietly damage a PATH, both of which mean "don't touch it":
REM   - it truncates at 1024 characters;
REM   - it writes REG_SZ, so a REG_EXPAND_SZ value containing %USERPROFILE% or
REM     the like comes back with those references already baked in.
set "PATHRISK="
call :strlen "!NEWPATH!" PLEN
if !PLEN! GEQ 1024 set "PATHRISK=it is !PLEN! characters long and setx truncates at 1024"
echo !USERPATH! | find "%%" >nul && set "PATHRISK=it contains %%-style references that setx would expand permanently"

if defined PATHRISK (
    call :warn "your user PATH was left alone: !PATHRISK!"
    call :addwarn "Add %HERE% to PATH by hand via System Properties > Environment Variables."
) else (
    setx PATH "!NEWPATH!" >nul
    if errorlevel 1 (
        call :warn "could not write the PATH variable"
        call :addwarn "Add %HERE% to PATH by hand via System Properties > Environment Variables."
    ) else (
        call :ok "added to your user PATH - open a NEW terminal for it to take effect"
    )
)
:path_done

REM ---------------------------------------------------------------------------
call :step "Verifying the install"
REM ---------------------------------------------------------------------------
set "PYTHONPATH=%HERE%"
set "G023_HOME=%HERE%"
"%PYTHON%" -c "import g023_code, g023_code.cli, g023_code.api, g023_code.orchestrator" 2>"%TEMP%\g023_import_err.txt"
if errorlevel 1 (
    call :fail "importing g023_code failed:"
    type "%TEMP%\g023_import_err.txt"
    del "%TEMP%\g023_import_err.txt" >nul 2>&1
    goto abort
)
del "%TEMP%\g023_import_err.txt" >nul 2>&1
call :ok "the package imports cleanly on %PYVER%"

echo.
echo   g023 Code is set up.
echo.
if not "%WARNCOUNT%"=="0" (
    echo   Before it will run:
    for /l %%I in (1,1,%WARNCOUNT%) do echo     ! !WARN%%I!
    echo.
)
echo   Start it from whatever project you want it to work on:
echo       cd C:\some\project
echo       g023
echo.
echo   ^(or %HERE%\g023.bat if you skipped the PATH step^)
echo.
echo   The folder you launch from is the project root; K.dat and config.json
echo   always stay here in %HERE%.
echo   First things to try: /help  /status  /tools  /vision
echo.
if "%ASSUME_YES%"=="0" pause
endlocal
exit /b 0

REM ===========================================================================
REM  Subroutines
REM ===========================================================================

:step
set /a STEP+=1
echo.
echo   [%STEP%] %~1
exit /b 0

:ok
echo       [ok] %~1
exit /b 0

:skip
echo       [--] %~1
exit /b 0

:warn
echo       [!!] %~1
exit /b 0

:fail
echo       [XX] %~1
exit /b 0

:addwarn
set /a WARNCOUNT+=1
set "WARN%WARNCOUNT%=%~1"
exit /b 0

:strlen
REM :strlen <string> <out-var>
set "S=%~1"
set "LEN=0"
:strlen_loop
if defined S (
    set "S=!S:~1!"
    set /a LEN+=1
    goto strlen_loop
)
set "%~2=%LEN%"
exit /b 0

:ask
REM :ask <question> <default y|n> -> ANSWER
set "ANSWER=%~2"
if "%ASSUME_YES%"=="1" exit /b 0
set "REPLY="
if /I "%~2"=="y" ( set "HINT=[Y/n]" ) else ( set "HINT=[y/N]" )
set /p "REPLY=      %~1 %HINT% "
if not defined REPLY exit /b 0
if /I "%REPLY:~0,1%"=="y" ( set "ANSWER=y" ) else ( set "ANSWER=n" )
exit /b 0

:probe
REM :probe <import name> <pip spec> - append to MISSING_OPTIONAL when absent
"%PYTHON%" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('%~1') else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    call :skip "%~1 already present"
) else (
    if defined MISSING_OPTIONAL ( set "MISSING_OPTIONAL=!MISSING_OPTIONAL! "%~2"" ) else ( set "MISSING_OPTIONAL="%~2"" )
)
exit /b 0

:need
REM :need <import name> <pip spec> <required|recommended>
"%PYTHON%" -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('%~1') else 1)" >nul 2>&1
if %ERRORLEVEL% equ 0 (
    call :skip "%~1 already present"
    exit /b 0
)
REM %2, not %~2: the quotes have to survive expansion or the >= in a version
REM pin is parsed as a redirection operator.
echo     pip install %2 ...
"%PYTHON%" -m pip install --disable-pip-version-check --quiet %2
if errorlevel 1 (
    if /I "%~3"=="required" (
        call :fail "could not install %~2"
        set "FATAL=1"
    ) else (
        call :warn "%~1 failed to install - the input line falls back to a plain prompt"
        call :addwarn "prompt_toolkit is missing: no tab completion or history."
    )
) else (
    call :ok "installed %~2"
)
exit /b 0

REM ===========================================================================
REM  Exits
REM ===========================================================================

:no_package
call :fail "this does not look like the g023 folder"
echo       Run installer.bat from the folder you unpacked g023 into
echo       ^(the one containing g023_code\ and requirements.txt^).
goto abort

:no_python
echo.
echo       No usable Python 3.11+ was found.
echo.
echo       Install it from https://www.python.org/downloads/windows/
echo       and tick "Add python.exe to PATH" in the installer,
echo       or run:  winget install Python.Python.3.12
echo.
echo       Then run installer.bat again.
goto abort

:uninstall
echo.
echo   g023 Code - removing the PATH entry
set "USERPATH="
for /f "usebackq tokens=2,*" %%A in (`reg query HKCU\Environment /v Path 2^>nul`) do set "USERPATH=%%B"
if not defined USERPATH (
    echo       [--] no user PATH set; nothing to remove
) else (
    call set "STRIPPED=%%USERPATH:;%HERE%=%%"
    call set "STRIPPED=%%STRIPPED:%HERE%;=%%"
    call set "STRIPPED=%%STRIPPED:%HERE%=%%"
    if "!STRIPPED!"=="!USERPATH!" (
        echo       [--] this folder was not on your user PATH
    ) else (
        setx PATH "!STRIPPED!" >nul
        echo       [ok] removed %HERE% from your user PATH
    )
)
echo.
echo   The project folder, .venv, K.dat and config.json were left alone.
echo   Delete the folder to finish removing g023.
echo.
if "%ASSUME_YES%"=="0" pause
endlocal
exit /b 0

:usage
echo.
echo   g023 Code installer
echo.
echo     installer.bat [options]
echo.
echo       /y            non-interactive; take the recommended default everywhere
echo       /key KEY      write KEY into K.dat instead of prompting
echo       /optional     also install the optional extras
echo       /nooptional   skip the optional extras
echo       /nopath       do not add this folder to your user PATH
echo       /uninstall    remove the PATH entry
echo       /?            this message
echo.
echo   Re-running is safe: each step is skipped when it is already done.
echo.
endlocal
exit /b 0

:abort
echo.
echo   Setup stopped.
echo.
if "%ASSUME_YES%"=="0" pause
endlocal
exit /b 1
