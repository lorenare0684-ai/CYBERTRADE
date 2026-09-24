@echo off
REM CYBERTRADE // NEON PROTOCOL — Miniconda setup (Windows) — FIXED for PATH issues
REM Creates conda env named cybertrade using Miniconda only
REM Works even if conda not in PATH — auto-detects Miniconda in common locations
REM Usage:
REM   setup_conda.bat
REM   conda activate cybertrade
REM   python -m cybertrade doctor

setlocal enabledelayedexpansion

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo [*] CYBERTRADE Miniconda setup for Windows
echo [*] Checking for conda (Miniconda)...

REM Try conda in PATH first
where conda >nul 2>&1
if %errorlevel% equ 0 (
    set CONDA_CMD=conda
    goto :found_conda
)

REM Not in PATH — search common Miniconda locations
echo [*] conda not in PATH — searching common Miniconda locations...

set FOUND_CONDA=

REM Check common locations for conda.exe / conda.bat
for %%P in (
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\Miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "C:\ProgramData\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\miniconda3\condabin\conda.bat"
    "%USERPROFILE%\Miniconda3\condabin\conda.bat"
    "C:\miniconda3\Scripts\conda.exe"
    "C:\Miniconda3\Scripts\conda.exe"
) do (
    if exist %%~P (
        echo [*] Found conda at %%~P
        set FOUND_CONDA=%%~P
        goto :found_file
    )
)

REM Also try via where in condabin which is often added to PATH by installer
if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" (
    set FOUND_CONDA=%USERPROFILE%\miniconda3\condabin\conda.bat
    goto :found_file
)
if exist "%USERPROFILE%\Miniconda3\condabin\conda.bat" (
    set FOUND_CONDA=%USERPROFILE%\Miniconda3\condabin\conda.bat
    goto :found_file
)

:found_file
if defined FOUND_CONDA (
    echo [*] Using FOUND_CONDA=%FOUND_CONDA%
    REM For conda.bat we need to call it, for conda.exe we can use directly
    set CONDA_CMD="%FOUND_CONDA%"
    REM Add its directory to PATH so conda activate works later
    for %%F in ("%FOUND_CONDA%") do set CONDA_DIR=%%~dpF
    set PATH=!CONDA_DIR!;%PATH%
    REM Also add parent Scripts and condabin
    set PATH=%USERPROFILE%\miniconda3\Scripts;%USERPROFILE%\miniconda3\condabin;%USERPROFILE%\Miniconda3\Scripts;%USERPROFILE%\Miniconda3\condabin;%PATH%
    goto :found_conda
)

REM Still not found — show helpful error
echo.
echo [!] conda NOT found — even after searching common locations
echo.
echo     Searched:
echo       %USERPROFILE%\miniconda3\Scripts\conda.exe
echo       %USERPROFILE%\Miniconda3\Scripts\conda.exe
echo       %LOCALAPPDATA%\miniconda3\Scripts\conda.exe
echo       C:\ProgramData\miniconda3\Scripts\conda.exe
echo       %USERPROFILE%\miniconda3\condabin\conda.bat
echo.
echo     FIX 1 (Recommended): Open Anaconda Prompt from Start Menu
echo       - Press Windows key, type "Anaconda Prompt", open it
echo       - In Anaconda Prompt, cd to this folder:
echo         cd /d "%CD%"
echo       - Then run: setup_conda.bat
echo.
echo     FIX 2: Add Miniconda to PATH
echo       - During Miniconda install, check "Add Miniconda3 to PATH environment variable"
echo       - Or manually add to PATH:
echo         %USERPROFILE%\miniconda3
echo         %USERPROFILE%\miniconda3\Scripts
echo         %USERPROFILE%\miniconda3\condabin
echo.
echo     FIX 3: Install Miniconda if not installed
echo       https://docs.anaconda.com/miniconda/install/
echo       Download: Miniconda3-latest-Windows-x86_64.exe
echo.
pause
exit /b 1

:found_conda
echo [*] Using conda: %CONDA_CMD%
call %CONDA_CMD% --version
if %errorlevel% neq 0 (
    echo [!] conda --version failed
    pause
    exit /b 1
)

if not exist "%ENV_FILE%" (
    echo [!] %ENV_FILE% not found in %CD%
    pause
    exit /b 1
)

echo [*] Checking if env %ENV_NAME% exists...
call %CONDA_CMD% env list | findstr /R /C:"^%ENV_NAME% " >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] Env %ENV_NAME% exists — updating from %ENV_FILE%
    call %CONDA_CMD% env update -n %ENV_NAME% -f %ENV_FILE% --prune
) else (
    echo [*] Creating env %ENV_NAME% from %ENV_FILE%
    call %CONDA_CMD% env create -f %ENV_FILE%
)

if %errorlevel% neq 0 (
    echo [!] Failed to create/update env
    echo     Try: conda clean --all
    echo     Then: conda env create -f %ENV_FILE% --force
    pause
    exit /b 1
)

echo.
echo [OK] Conda env %ENV_NAME% ready (Miniconda).
echo.
echo     To activate (in Anaconda Prompt):
echo       conda activate %ENV_NAME%
echo.
echo     Then verify:
echo       python -m cybertrade doctor
echo       python -m unittest discover -s tests
echo       python -m cybertrade web --port 8899 --auto
echo       python run_gui.py
echo.
echo     If conda activate says "not recognized" in normal CMD:
echo       Always use Anaconda Prompt from Start Menu
echo.
echo     To deactivate:
echo       conda deactivate
echo.
echo     To remove:
echo       conda env remove -n %ENV_NAME%
echo.
pause
endlocal
