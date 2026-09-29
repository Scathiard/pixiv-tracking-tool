@echo off
cd /d "%~dp0"

REM Build IDs and public version come from src/version.py.
REM No cleanup, config copying or replacement of an existing build.
if exist "..\.devtools\v012-env\Scripts\python.exe" (
    "..\.devtools\v012-env\Scripts\python.exe" build_release.py %*
) else (
    py -3.12 build_release.py %*
)
exit /b %ERRORLEVEL%
