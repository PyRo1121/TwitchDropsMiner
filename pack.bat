@echo off
setlocal

set "script_dir=%~dp0"
set "release_dir=%script_dir%Twitch Drops Miner"
set "archive=%script_dir%Twitch Drops Miner.zip"

where 7z.exe >nul 2>&1
if errorlevel 1 (
    echo No 7z.exe detected in PATH, skipping packaging.
    exit /b 1
)

if not exist "%release_dir%" mkdir "%release_dir%"
copy /y /v "%script_dir%dist\*.exe" "%release_dir%\" >nul
if errorlevel 1 exit /b 1
copy /y /v "%script_dir%manual.txt" "%release_dir%\" >nul
if errorlevel 1 exit /b 1

7z.exe a -tzip "%archive%" "%release_dir%\*" -r
if errorlevel 1 exit /b 1
7z.exe t "%archive%"
set "pack_status=%errorlevel%"

if exist "%release_dir%" rmdir /s /q "%release_dir%"
exit /b %pack_status%
