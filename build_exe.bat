@echo off
setlocal

cd /d "%~dp0"

set "MODE=%~1"
if /i "%MODE%"=="" set "MODE=onefile"

if /i not "%MODE%"=="onefile" if /i not "%MODE%"=="onedir" (
    echo Invalid build mode "%MODE%".
    echo Use: build_exe.bat [onefile^|onedir]
    echo.
    pause
    exit /b 1
)

where powershell >nul 2>nul
if errorlevel 1 (
    echo PowerShell was not found on PATH.
    echo.
    pause
    exit /b 1
)

echo Building Crimson Forge Toolkit in %MODE% mode...
echo.

powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_pyside6_app.ps1" -Mode %MODE%
set "EXITCODE=%ERRORLEVEL%"

echo.
if not "%EXITCODE%"=="0" (
    echo Build failed with exit code %EXITCODE%.
    echo.
    pause
    exit /b %EXITCODE%
)

echo Build finished successfully.
echo.
pause
exit /b 0
