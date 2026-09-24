@echo off
REM Activate cybertrade conda env on Windows
REM Usage: call activate_cybertrade.bat  (or double-click)
REM Then: python -m cybertrade doctor

set ENV_NAME=cybertrade

echo [*] Activating conda env %ENV_NAME%...

REM Try conda activate
where conda >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] Found conda, activating...
    call conda activate %ENV_NAME%
    if %errorlevel% equ 0 (
        echo [OK] Activated via conda
        python --version
        python -m cybertrade doctor
        goto :eof
    )
)

REM Fallback: check if env exists in default locations
if exist "%USERPROFILE%\miniforge3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at %USERPROFILE%\miniforge3\envs\%ENV_NAME%
    set PATH=%USERPROFILE%\miniforge3\envs\%ENV_NAME%;%USERPROFILE%\miniforge3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=%USERPROFILE%\miniforge3\envs\%ENV_NAME%
    python --version
    python -m cybertrade doctor
    goto :eof
)

if exist "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at %USERPROFILE%\Miniconda3\envs\%ENV_NAME%
    set PATH=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%;%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%
    python --version
    python -m cybertrade doctor
    goto :eof
)

if exist "%LOCALAPPDATA%\miniforge3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at %LOCALAPPDATA%\miniforge3\envs\%ENV_NAME%
    set PATH=%LOCALAPPDATA%\miniforge3\envs\%ENV_NAME%;%LOCALAPPDATA%\miniforge3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=%LOCALAPPDATA%\miniforge3\envs\%ENV_NAME%
    python --version
    python -m cybertrade doctor
    goto :eof
)

echo [!] Conda env %ENV_NAME% not found
echo     Create it with:
echo       conda env create -f environment.yml
echo     Or run:
echo       setup_conda.bat
pause
