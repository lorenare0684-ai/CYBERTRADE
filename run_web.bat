@echo off
REM CYBERTRADE — Launch browser terminal on Windows
REM Usage: run_web.bat
REM Opens http://localhost:8899

call conda activate cybertrade 2>nul
if %errorlevel% neq 0 (
    echo [*] Trying to activate via PATH...
    if exist "%USERPROFILE%\miniforge3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\miniforge3\envs\cybertrade%;%USERPROFILE%\miniforge3\envs\cybertrade\Scripts;%PATH%
    )
)

echo [*] Starting CYBERTRADE web terminal on http://localhost:8899
echo [*] Press Ctrl+C to stop
python -m cybertrade web --port 8899 --auto %*
pause
