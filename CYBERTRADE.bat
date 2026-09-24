@echo off
REM CYBERTRADE // NEON PROTOCOL — ONE-CLICK LAUNCHER (Miniconda, no Anaconda Prompt needed)
REM Just double-click this file — it finds Miniconda, creates env if needed, and runs web HUD
REM No need to open Anaconda Prompt, no need to add conda to PATH

setlocal enabledelayedexpansion

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo ========================================================
echo  CYBERTRADE // NEON PROTOCOL — ONE-CLICK START
echo  Miniconda only, no Anaconda Prompt needed
echo  Just double-click — auto-finds Miniconda, creates env, runs
echo ========================================================
echo.

REM Step 1: Find conda.exe (Miniconda) even if not in PATH
set CONDA_EXE=
set MINICONDA_ROOT=

echo [*] Searching for Miniconda...

for %%P in (
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\Miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "C:\ProgramData\miniconda3\Scripts\conda.exe"
    "C:\miniconda3\Scripts\conda.exe"
    "C:\Miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\miniconda3\condabin\conda.bat"
    "%USERPROFILE%\Miniconda3\condabin\conda.bat"
) do (
    if exist %%~P (
        set CONDA_EXE=%%~P
        for %%F in ("%%~P") do set MINICONDA_ROOT=%%~dpF\..
        echo [*] Found Miniconda: !CONDA_EXE!
        goto :found_conda
    )
)

REM Also try conda in PATH as last resort
where conda >nul 2>&1
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('where conda') do set CONDA_EXE=%%i
    echo [*] Found conda in PATH: !CONDA_EXE!
    goto :found_conda
)

echo.
echo [!] Miniconda NOT found
echo.
echo     Please install Miniconda first (one time):
echo     https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe
echo     Download and run installer, choose "Just Me", keep defaults
echo.
echo     After install, double-click this file again: CYBERTRADE.bat
echo.
pause
exit /b 1

:found_conda
echo [*] Using: %CONDA_EXE%

REM Step 2: Check if env exists, if not create it
echo [*] Checking for env %ENV_NAME%...

REM Find env python to check existence
set ENV_PYTHON=
for %%P in (
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\miniconda3\envs\%ENV_NAME%\python.exe"
) do (
    if exist %%~P set ENV_PYTHON=%%~P
)

if not defined ENV_PYTHON (
    echo [*] Env %ENV_NAME% not found — creating (first time, 1-2 min)...
    echo [*] Running: conda env create -f %ENV_FILE%
    call "%CONDA_EXE%" env create -f "%ENV_FILE%"
    if %errorlevel% neq 0 (
        echo [!] Failed to create env. Trying with --force...
        call "%CONDA_EXE%" env create -f "%ENV_FILE%" --force
        if %errorlevel% neq 0 (
            echo [!] Still failed. Try in Anaconda Prompt:
            echo     conda clean --all
            echo     conda env create -f %ENV_FILE% --force
            pause
            exit /b 1
        )
    )
    echo [OK] Env %ENV_NAME% created
) else (
    echo [*] Env %ENV_NAME% exists at !ENV_PYTHON!
)

REM Step 3: Find env python again after creation
set ENV_PYTHON=
for %%P in (
    "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe"
    "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe"
    "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\miniconda3\envs\%ENV_NAME%\python.exe"
    "C:\Miniconda3\envs\%ENV_NAME%\python.exe"
) do (
    if exist %%~P set ENV_PYTHON=%%~P
)

if not defined ENV_PYTHON (
    echo [!] Env python not found after creation
    pause
    exit /b 1
)

for %%F in ("%ENV_PYTHON%") do set ENV_DIR=%%~dpF
for %%F in ("%ENV_DIR%..") do set ENV_ROOT=%%~fF

echo [*] Env python: %ENV_PYTHON%
echo [*] Env root: %ENV_ROOT%

REM Step 4: Activate env by setting PATH (no conda activate needed, works in normal CMD)
set PATH=%ENV_ROOT%;%ENV_ROOT%\Scripts;%ENV_ROOT%\Library\bin;%PATH%
set CONDA_DEFAULT_ENV=%ENV_NAME%
set CONDA_PREFIX=%ENV_ROOT%

echo [*] Activated env %ENV_NAME% via PATH (no Anaconda Prompt needed)
python --version
python -m cybertrade doctor

echo.
echo ========================================================
echo  [OK] Starting CYBERTRADE web terminal...
echo  Browser will open at http://localhost:8899
echo  Press Ctrl+C to stop, close window to exit
echo ========================================================
echo.

REM Step 5: Run web terminal (one-click)
REM --auto will open browser automatically
python -m cybertrade web --port 8899 --auto

pause
endlocal
