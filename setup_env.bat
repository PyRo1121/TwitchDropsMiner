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

python -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 10))"
if errorlevel 1 (
    echo:
    echo Release builds require CPython 3.10.
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

"%venv_dir%\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 10))"
if errorlevel 1 (
    echo:
    echo Existing .venv does not use CPython 3.10; replace it deliberately.
    echo:
    pause
    exit /b 1
)

echo:
echo Installing hash-locked packaging bootstrap...
"%venv_dir%\Scripts\python.exe" -m pip install --require-hashes --only-binary=:all: -r "%script_dir%requirements-bootstrap.txt"
if errorlevel 1 (
    echo:
    echo Failed to install hash-locked packaging bootstrap.
    echo:
    pause
    exit /b 1
)

echo:
echo Installing hash-locked build dependencies...
"%venv_dir%\Scripts\python.exe" -m pip install --require-hashes --only-binary=:all: -r "%script_dir%requirements-build.txt"
if errorlevel 1 (
    echo:
    echo Failed to install hash-locked build dependencies.
    echo:
    pause
    exit /b 1
)

echo:
echo Environment setup completed successfully.
echo:
pause
exit /b 0
