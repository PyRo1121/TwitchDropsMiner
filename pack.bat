@echo off
setlocal

set "script_dir=%~dp0"
set "python=%script_dir%.venv\Scripts\python.exe"
set "executable=%script_dir%dist\Twitch Drops Miner (by DevilXD).exe"
set "archive=%script_dir%Twitch Drops Miner.zip"

if not exist "%python%" (
    echo No build environment found. Run setup_env.bat first.
    exit /b 1
)
if not exist "%executable%" (
    echo No frozen Windows executable found. Run build.bat first.
    exit /b 1
)
if not defined SOURCE_DATE_EPOCH set "SOURCE_DATE_EPOCH=0"

"%python%" "%script_dir%build_tools\package_release.py" ^
    --output "%archive%" ^
    --entry "%executable%=Twitch Drops Miner/Twitch Drops Miner (by DevilXD).exe" ^
    --entry "%script_dir%manual.txt=Twitch Drops Miner/manual.txt"
if errorlevel 1 exit /b 1

"%python%" -m zipfile -t "%archive%"
exit /b %errorlevel%
