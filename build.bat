@echo off
setlocal

set "script_dir=%~dp0"
set "venv_dir=%script_dir%.venv"
set "pyinstaller=%venv_dir%\Scripts\pyinstaller.exe"

if not defined PYTHONHASHSEED set "PYTHONHASHSEED=0"
if not defined SOURCE_DATE_EPOCH set "SOURCE_DATE_EPOCH=0"

if not exist "%pyinstaller%" (
    echo:
    echo No build environment found. Run setup_env.bat first.
    echo:
    if /I not "%~1"=="--nopause" pause
    exit /b 1
)

pushd "%script_dir%" >nul
if errorlevel 1 exit /b 1

echo Building...
"%pyinstaller%" --clean --noconfirm "%script_dir%build.spec"
set "build_status=%errorlevel%"
popd

if not "%build_status%"=="0" (
    echo:
    echo PyInstaller build failed.
    echo:
    if /I not "%~1"=="--nopause" pause
    exit /b %build_status%
)

echo:
echo Build completed successfully.
echo:
if /I not "%~1"=="--nopause" pause
exit /b 0
