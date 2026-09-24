@echo off
REM Activate cybertrade conda env on Windows (Miniconda only)
REM Usage: call activate_cybertrade.bat  (or double-click)
REM Then: python -m cybertrade doctor

set ENV_NAME=cybertrade

echo [*] Activating conda env %ENV_NAME% (Miniconda)...

REM Try conda activate (Miniconda)
where conda >nul 2>&1
if %errorlevel% equ 0 (
    echo [*] Found conda (Miniconda), activating...
    call conda activate %ENV_NAME%
    if %errorlevel% equ 0 (
        echo [OK] Activated via Miniconda
        python --version
        python -m cybertrade doctor
        goto :eof
    )
)

REM Fallback: check if env exists in default Miniconda locations
if exist "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at %USERPROFILE%\miniconda3\envs\%ENV_NAME%
    set PATH=%USERPROFILE%\miniconda3\envs\%ENV_NAME%;%USERPROFILE%\miniconda3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=%USERPROFILE%\miniconda3\envs\%ENV_NAME%
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

if exist "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at %LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%
    set PATH=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%;%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%
    python --version
    python -m cybertrade doctor
    goto :eof
)

if exist "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe" (
    echo [*] Found env at C:\ProgramData\miniconda3\envs\%ENV_NAME%
    set PATH=C:\ProgramData\miniconda3\envs\%ENV_NAME%;C:\ProgramData\miniconda3\envs\%ENV_NAME%\Scripts;%PATH%
    set CONDA_DEFAULT_ENV=%ENV_NAME%
    set CONDA_PREFIX=C:\ProgramData\miniconda3\envs\%ENV_NAME%
    python --version
    python -m cybertrade doctor
    goto :eof
)

echo [!] Conda env %ENV_NAME% not found (Miniconda)
echo     Create it with:
echo       conda env create -f environment.yml
echo     Or run:
echo       setup_conda.bat
echo.
echo     Install Miniconda if not installed:
echo       https://docs.anaconda.com/miniconda/install/
pause
