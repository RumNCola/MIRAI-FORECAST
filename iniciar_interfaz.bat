@echo off
setlocal EnableExtensions DisableDelayedExpansion
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

".venv\Scripts\python.exe" -m streamlit run "streamlit_app.py"
set "MIRAI_EXIT_CODE=%errorlevel%"
popd
exit /b %MIRAI_EXIT_CODE%

:missing_python
echo ERROR: Install Python 3.12 or newer and run iniciar_interfaz.bat again.
goto failed

:invalid_environment
echo ERROR: The existing .venv is broken or uses an unsupported Python version.
echo Repair or recreate that environment, then run main.bat again.
goto failed

:failed
echo ERROR: Execution stopped. See pipeline.log for setup and analysis details.
pause
popd
exit /b 1
