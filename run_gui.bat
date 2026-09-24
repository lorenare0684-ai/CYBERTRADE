@echo off
REM CYBERTRADE — Launch desktop GUI on Windows (Miniconda env cybertrade)
REM Requires Miniconda env cybertrade with tk

call conda activate cybertrade 2>nul
if %errorlevel% neq 0 (
    echo [*] Trying Miniconda PATH fallback...
    if exist "%USERPROFILE%\miniconda3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\miniconda3\envs\cybertrade%;%USERPROFILE%\miniconda3\envs\cybertrade\Scripts;%PATH%
    ) else if exist "%USERPROFILE%\Miniconda3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\Miniconda3\envs\cybertrade%;%USERPROFILE%\Miniconda3\envs\cybertrade\Scripts;%PATH%
    )
)

python run_gui.py %*
pause
