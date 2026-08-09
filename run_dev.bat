@echo off
setlocal

set "script_dir=%~dp0"
set "venv_dir=%script_dir%.venv"
set /p "choice=Start with a console? (y/n) "
if /I "%choice%"=="y" (
    set "python=%venv_dir%\Scripts\python.exe"
) else (
    set "python=%venv_dir%\Scripts\pythonw.exe"
)

if not exist "%python%" (
    echo No development environment found. Run setup_env.bat first.
    exit /b 1
)

start "TwitchDropsMiner" "%python%" "%script_dir%main.py"
exit /b 0
