@echo off
setlocal

set "script_dir=%~dp0"
set "venv_dir=%script_dir%.venv"

where python.exe >nul 2>&1
if errorlevel 1 (
    echo:
    echo No Python executable found in PATH.
    echo:
    pause
    exit /b 1
)

if not exist "%venv_dir%\Scripts\python.exe" (
    echo:
    echo Creating %venv_dir%...
    python -m venv "%venv_dir%"
    if errorlevel 1 (
        echo Failed to create the virtual environment.
        pause
        exit /b 1
    )
)

echo:
echo Installing locked build dependencies...
"%venv_dir%\Scripts\python.exe" -m pip install -r "%script_dir%requirements-build.txt"
if errorlevel 1 (
    echo:
    echo Failed to install locked build dependencies.
    echo:
    pause
    exit /b 1
)

echo:
echo Environment setup completed successfully.
echo:
pause
exit /b 0
