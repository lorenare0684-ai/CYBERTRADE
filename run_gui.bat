@echo off
REM CYBERTRADE — Launch desktop GUI on Windows
REM Requires conda env cybertrade with tk

call conda activate cybertrade 2>nul
if %errorlevel% neq 0 (
    echo [*] Trying to activate via PATH...
    if exist "%USERPROFILE%\miniforge3\envs\cybertrade\python.exe" (
        set PATH=%USERPROFILE%\miniforge3\envs\cybertrade%;%USERPROFILE%\miniforge3\envs\cybertrade\Scripts;%PATH%
    )
)

python run_gui.py %*
pause
