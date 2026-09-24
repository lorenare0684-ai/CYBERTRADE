@echo off
REM CYBERTRADE // NEON PROTOCOL — Windows conda setup
REM Creates conda env named cybertrade on Windows
REM Usage:
REM   setup_conda.bat
REM   conda activate cybertrade
REM   python -m cybertrade doctor

setlocal enabledelayedexpansion

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo [*] CYBERTRADE conda setup for Windows
echo [*] Checking for conda...

where conda >nul 2>&1
if %errorlevel% neq 0 (
    where mamba >nul 2>&1
    if %errorlevel% neq 0 (
        where micromamba >nul 2>&1
        if %errorlevel% neq 0 (
            echo [!] conda / mamba / micromamba not found in PATH
            echo     Install Miniforge for Windows:
            echo     https://github.com/conda-forge/miniforge
            echo     Download Miniforge3-Windows-x86_64.exe and run installer
            echo     Then open "Miniforge Prompt" from Start Menu and re-run this script
            echo.
            echo     Or install Miniconda:
            echo     https://docs.anaconda.com/miniconda/install/
            pause
            exit /b 1
        ) else (
            set CONDA_BIN=micromamba
        )
    ) else (
        set CONDA_BIN=mamba
    )
) else (
    set CONDA_BIN=conda
)

echo [*] Using %CONDA_BIN%
call %CONDA_BIN% --version

if not exist "%ENV_FILE%" (
    echo [!] %ENV_FILE% not found in %CD%
    pause
    exit /b 1
)

echo [*] Checking if env %ENV_NAME% exists...
call %CONDA_BIN% env list | findstr /R /C:"^%ENV_NAME% " >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] Env %ENV_NAME% exists — updating from %ENV_FILE%
    call %CONDA_BIN% env update -n %ENV_NAME% -f %ENV_FILE% --prune
) else (
    echo [*] Creating env %ENV_NAME% from %ENV_FILE%
    call %CONDA_BIN% env create -f %ENV_FILE%
)

if %errorlevel% neq 0 (
    echo [!] Failed to create/update env
    pause
    exit /b 1
)

echo.
echo [OK] Conda env %ENV_NAME% ready.
echo.
echo     To activate:
echo       conda activate %ENV_NAME%
echo.
echo     Then verify:
echo       python -m cybertrade doctor
echo       python -m unittest discover -s tests
echo       python -m cybertrade web --port 8899 --auto
echo       python run_gui.py
echo.
echo     To deactivate:
echo       conda deactivate
echo.
echo     To remove:
echo       conda env remove -n %ENV_NAME%
echo.
pause
endlocal
