@echo off
REM CYBERTRADE — Launch browser terminal on Windows (Miniconda env cybertrade)
REM Usage: run_web.bat
REM Opens http://localhost:8899

call conda activate cybertrade 2>nul
if %errorlevel% neq 0 (
    echo [*] Trying Miniconda PATH fallback...
    if exist "%USERPROFILE%\miniconda3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\miniconda3\envs\cybertrade%;%USERPROFILE%\miniconda3\envs\cybertrade\Scripts;%PATH%
    ) else if exist "%USERPROFILE%\Miniconda3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\Miniconda3\envs\cybertrade%;%USERPROFILE%\Miniconda3\envs\cybertrade\Scripts;%PATH%
    )
)

echo [*] Starting CYBERTRADE web terminal on http://localhost:8899 (Miniconda env cybertrade)
echo [*] Press Ctrl+C to stop
python -m cybertrade web --port 8899 --auto %*
pause
