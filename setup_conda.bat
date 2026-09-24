@echo off
REM CYBERTRADE // NEON PROTOCOL — Miniconda setup (Windows)
REM Creates conda env named cybertrade using Miniconda only
REM Usage:
REM   setup_conda.bat
REM   conda activate cybertrade
REM   python -m cybertrade doctor

setlocal

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo [*] CYBERTRADE Miniconda setup for Windows
echo [*] Checking for conda (Miniconda)...

where conda >nul 2>&1
if %errorlevel% neq 0 (
    echo [!] conda not found in PATH
    echo     Install Miniconda for Windows:
    echo     https://docs.anaconda.com/miniconda/install/
    echo     Download Miniconda3-latest-Windows-x86_64.exe and run installer
    echo     Then open "Anaconda Prompt" from Start Menu and re-run this script
    echo.
    echo     Make sure to check "Add Miniconda3 to PATH environment variable" during install
    echo     or use Anaconda Prompt which has conda in PATH automatically
    pause
    exit /b 1
)

echo [*] Using conda
call conda --version

if not exist "%ENV_FILE%" (
    echo [!] %ENV_FILE% not found in %CD%
    pause
    exit /b 1
)

echo [*] Checking if env %ENV_NAME% exists...
call conda env list | findstr /R /C:"^%ENV_NAME% " >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] Env %ENV_NAME% exists — updating from %ENV_FILE%
    call conda env update -n %ENV_NAME% -f %ENV_FILE% --prune
) else (
    echo [*] Creating env %ENV_NAME% from %ENV_FILE%
    call conda env create -f %ENV_FILE%
)

if %errorlevel% neq 0 (
    echo [!] Failed to create/update env
    pause
    exit /b 1
)

echo.
echo [OK] Conda env %ENV_NAME% ready (Miniconda).
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
