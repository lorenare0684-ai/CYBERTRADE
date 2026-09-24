@echo off
REM Fix conda not found in PATH — Miniconda only
REM This script searches for Miniconda and adds it to PATH for current session
REM and offers to init conda for future sessions

echo [*] CYBERTRADE — Fix conda PATH (Miniconda only)
echo.

set FOUND_CONDA=

for %%P in (
    "%USERPROFILE%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\Miniconda3\Scripts\conda.exe"
    "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe"
    "%USERPROFILE%\anaconda3\Scripts\conda.exe"
    "C:\ProgramData\miniconda3\Scripts\conda.exe"
    "C:\miniconda3\Scripts\conda.exe"
) do (
    if exist %%~P (
        echo [*] Found conda at %%~P
        set FOUND_CONDA=%%~P
        goto :found
    )
)

:found
if not defined FOUND_CONDA (
    echo [!] Miniconda not found in common locations
    echo     Please install Miniconda:
    echo     https://docs.anaconda.com/miniconda/install/
    echo     Download: Miniconda3-latest-Windows-x86_64.exe
    pause
    exit /b 1
)

echo [*] Found: %FOUND_CONDA%
for %%F in ("%FOUND_CONDA%") do set CONDA_DIR=%%~dpF
echo [*] Conda dir: %CONDA_DIR%

REM Add to PATH for current session
set PATH=%CONDA_DIR%;%CONDA_DIR%\..\condabin;%CONDA_DIR%\..;%PATH%
echo [*] Added to PATH for this session
call conda --version

echo.
echo [*] To make conda available in future normal CMD/PowerShell sessions:
echo     Option 1 (Recommended): Always use Anaconda Prompt from Start Menu
echo       - Windows key -> type "Anaconda Prompt" -> open
echo       - conda is already in PATH there
echo.
echo     Option 2: Init conda for your shell
echo       - In this Anaconda Prompt, run:
echo         conda init cmd.exe
echo         conda init powershell
echo       - Then restart CMD/PowerShell
echo.
echo     Option 3: Manually add to System PATH (not recommended but works)
echo       - Add these to PATH in Environment Variables:
echo         %USERPROFILE%\miniconda3
echo         %USERPROFILE%\miniconda3\Scripts
echo         %USERPROFILE%\miniconda3\condabin
echo.
echo [*] Now try:
echo       setup_conda.bat
echo       conda env create -f environment.yml
echo.
pause
