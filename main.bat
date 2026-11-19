@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem Usage: main.bat [--comenzar-hoy]
rem Runs ELT, the demand models of every site in settings/config.py (Franklin, MUT, Franklin store),
rem the production report and the inventory. Exports go in Data/RAW/<SOURCE>/ (store: FRANKLIN_STORE).
rem When opened with a double-click, the window waits for a key at the end so messages can be read.
rem Set MIRAI_NO_PAUSE=1 to never wait (scheduled tasks, scripts).
set "MIRAI_SCRIPT=%~nx0"
pushd "%~dp0"
if errorlevel 1 exit /b 1

if exist ".venv\Scripts\python.exe" goto validate_environment

where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto use_python
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto validate_environment

:use_python
where python >nul 2>nul
if errorlevel 1 goto missing_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto missing_python
python -m venv ".venv"
if errorlevel 1 goto failed

:validate_environment
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 goto invalid_environment
set "PYTHONUTF8=1"

".venv\Scripts\python.exe" "scripts\bootstrap.py"
if errorlevel 1 goto failed

".venv\Scripts\python.exe" "main.py" %*
set "MIRAI_EXIT_CODE=%errorlevel%"
popd
if not "%MIRAI_EXIT_CODE%"=="0" echo ERROR: Execution stopped. See pipeline.log for the details.
call :pause_if_double_clicked
exit /b %MIRAI_EXIT_CODE%

:missing_python
echo ERROR: Install Python 3.12 or newer and run main.bat again.
goto failed

:invalid_environment
echo ERROR: The existing .venv is broken or uses an unsupported Python version.
echo Repair or recreate that environment, then run main.bat again.
goto failed

:failed
echo ERROR: Execution stopped. See pipeline.log for setup and analysis details.
popd
call :pause_if_double_clicked
exit /b 1

:pause_if_double_clicked
rem A double-click opens a new window just for this script ("cmd.exe /c ...main.bat"); a terminal
rem the user typed into does not mention it. Only the first case closes before messages can be read.
if defined MIRAI_NO_PAUSE exit /b 0
echo(%cmdcmdline%| findstr /i /l /c:"%MIRAI_SCRIPT%" >nul || exit /b 0
echo.
pause
exit /b 0
