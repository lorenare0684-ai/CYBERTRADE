@echo off
REM CYBERTRADE — ONE-CLICK (Miniconda, no Anaconda Prompt) — FIXED for "was unexpected" error
REM Just double-click — finds Miniconda, creates env if needed, runs web HUD

setlocal

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo ========================================================
echo  CYBERTRADE — ONE-CLICK START (Miniconda, fixed)
echo  Just double-click — auto-finds Miniconda, creates env, runs
echo ========================================================
echo.

REM Step 1: Find conda.exe — check common locations one by one (no for+goto)
set CONDA_EXE=

if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\Miniconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\Miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe" set CONDA_EXE=%LOCALAPPDATA%\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\anaconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "C:\ProgramData\miniconda3\Scripts\conda.exe" set CONDA_EXE=C:\ProgramData\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "C:\miniconda3\Scripts\conda.exe" set CONDA_EXE=C:\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" set CONDA_EXE=%USERPROFILE%\miniconda3\condabin\conda.bat
if not defined CONDA_EXE if exist "%USERPROFILE%\Miniconda3\condabin\conda.bat" set CONDA_EXE=%USERPROFILE%\Miniconda3\condabin\conda.bat

REM Fallback: try conda in PATH
if not defined CONDA_EXE (
    where conda >nul 2>&1
    if %errorlevel% equ 0 set CONDA_EXE=conda
)

if not defined CONDA_EXE (
    echo [!] Miniconda NOT found
    echo     Install: https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe
    echo     Then double-click CYBERTRADE.bat again
    pause
    exit /b 1
)

echo [*] Found Miniconda: %CONDA_EXE%
call "%CONDA_EXE%" --version

REM Step 2: Check if env exists by looking for python.exe
echo [*] Checking for env %ENV_NAME%...

set ENV_PYTHON=

if exist "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\miniconda3\envs\%ENV_NAME%\python.exe

if not defined ENV_PYTHON (
    echo [*] Env %ENV_NAME% not found — creating (first time, 1-2 min)...
    echo [*] Running: conda env create -f %ENV_FILE%
    call "%CONDA_EXE%" env create -f "%ENV_FILE%"
    if %errorlevel% neq 0 (
        echo [!] Failed, trying with --force...
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
    REM Find python again after creation
    if exist "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe
    if not defined ENV_PYTHON if exist "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe
    if not defined ENV_PYTHON if exist "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe
    if not defined ENV_PYTHON if exist "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe
) else (
    echo [*] Env %ENV_NAME% exists at %ENV_PYTHON%
)

if not defined ENV_PYTHON (
    echo [!] Env python not found after creation
    pause
    exit /b 1
)

echo [*] Env python: %ENV_PYTHON%

REM Get env root from python path (remove \python.exe)
set ENV_ROOT=%ENV_PYTHON%
set ENV_ROOT=%ENV_ROOT:\python.exe=%
REM Now ENV_ROOT is ...\envs\cybertrade, need to handle \Scripts\python.exe case? Already handled
REM Actually python.exe is in env root, so ENV_ROOT should be dir of python.exe
REM Let's get dir via for loop without goto
for %%F in ("%ENV_PYTHON%") do set ENV_ROOT=%%~dpF
REM Remove trailing backslash
if "%ENV_ROOT:~-1%"=="\" set ENV_ROOT=%ENV_ROOT:~0,-1%

echo [*] Env root: %ENV_ROOT%

REM Step 3: Activate by PATH (no conda activate needed)
set PATH=%ENV_ROOT%;%ENV_ROOT%\Scripts;%ENV_ROOT%\Library\bin;%PATH%
set CONDA_DEFAULT_ENV=%ENV_NAME%
set CONDA_PREFIX=%ENV_ROOT%

echo [*] Activated env %ENV_NAME% via PATH
python --version
python -m cybertrade doctor

echo.
echo ========================================================
echo  [OK] Starting CYBERTRADE web terminal...
echo  Browser will open at http://localhost:8899
echo  Press Ctrl+C to stop
echo ========================================================
echo.

python -m cybertrade web --port 8899 --auto

pause
endlocal
