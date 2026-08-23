@echo off
cd /d "%~dp0"

REM 从唯一版本源读取版本号
for /f "tokens=2 delims== " %%a in ('findstr /b "__version__" src\version.py') do set "VERSION=%%~a"
if not defined VERSION (
    echo VERSION READ FAILED
    exit /b 1
)
set "EXE_NAME=PixivTracker_v%VERSION%"
set "RELEASE_DIR=..\release\v%VERSION%"

echo ================================
echo   Pixiv Tracker Release Builder
echo   Version: %VERSION%
echo ================================
echo.

echo [1/3] Cleaning...
if exist "build_tmp" rmdir /s /q "build_tmp"
if exist "dist" rmdir /s /q "dist"

echo [2/3] Building EXE...
pyinstaller build.spec --distpath ./dist --workpath ./build_tmp --clean
if %ERRORLEVEL% neq 0 (
    echo BUILD FAILED
    pause
    exit /b 1
)

echo [3/3] Creating release: %RELEASE_DIR%
if not exist "%RELEASE_DIR%" mkdir "%RELEASE_DIR%"
copy /y "dist\%EXE_NAME%.exe" "%RELEASE_DIR%\"
copy /y .env.example "%RELEASE_DIR%\"

echo.
echo ================================
echo   Release complete!
echo   %RELEASE_DIR%\%EXE_NAME%.exe
echo ================================
pause
