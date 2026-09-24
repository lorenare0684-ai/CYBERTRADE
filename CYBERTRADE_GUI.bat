@echo off
REM CYBERTRADE // NEON PROTOCOL — ONE-CLICK GUI LAUNCHER (Miniconda, no Anaconda Prompt)
REM Just double-click — auto-finds Miniconda, creates env if needed, runs desktop GUI

setlocal enabledelayedexpansion

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo ========================================================
echo  CYBERTRADE // NEON PROTOCOL — ONE-CLICK GUI
echo  Miniconda only, no Anaconda Prompt needed
echo ========================================================
echo.

set CONDA_EXE=

for %%P in (
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\Miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "C:\ProgramData\miniconda3\Scripts\conda.exe"
    "C:\miniconda3\Scripts\conda.exe"
) do (
    if exist %%~P (
        set CONDA_EXE=%%~P
        goto :found_conda
    )
)

where conda >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where conda') do set CONDA_EXE=%%i
    goto :found_conda
)

echo [!] Miniconda NOT found — install from:
echo https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe
pause
exit /b 1

:found_conda
set ENV_PYTHON=
for %%P in (
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
) do (
    if exist %%~P set ENV_PYTHON=%%~P
)

if not defined ENV_PYTHON (
    echo [*] Env %ENV_NAME% not found — creating...
    call "%CONDA_EXE%" env create -f "%ENV_FILE%"
)

set ENV_PYTHON=
for %%P in (
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
) do (
    if exist %%~P set ENV_PYTHON=%%~P
)

for %%F in ("%ENV_PYTHON%") do set ENV_DIR=%%~dpF
for %%F in ("%ENV_DIR%..") do set ENV_ROOT=%%~fF

set PATH=%ENV_ROOT%;%ENV_ROOT%\Scripts;%ENV_ROOT%\Library\bin;%PATH%
set CONDA_DEFAULT_ENV=%ENV_NAME%
set CONDA_PREFIX=%ENV_ROOT%

echo [*] Activated %ENV_NAME% — launching desktop GUI...
python run_gui.py

pause
endlocal
