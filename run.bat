@echo off
cd /d "%~dp0"
pushd "%~dp0front_end"
call npm run build
if errorlevel 1 (
    echo Frontend build failed.
    popd
    pause
    exit /b 1
)
popd

start "fastapi" "%~dp0python\python.exe" "%~dp0main.py"
start "huey" "%~dp0python\python.exe" "%~dp0worker.py"
