@echo off
REM CYBERTRADE ONE-CLICK - Miniconda - No Anaconda Prompt - FIXED v2
REM Just double-click

setlocal

set ENV_NAME=cybertrade
set ENV_FILE=environment.yml

echo ========================================================
echo  CYBERTRADE ONE-CLICK START - Miniconda fixed v2
echo  Just double-click - auto-finds Miniconda, creates env, runs
echo ========================================================
echo.

set CONDA_EXE=

if exist "%USERPROFILE%\miniconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\Miniconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\Miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%LOCALAPPDATA%\miniconda3\Scripts\conda.exe" set CONDA_EXE=%LOCALAPPDATA%\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\anaconda3\Scripts\conda.exe" set CONDA_EXE=%USERPROFILE%\anaconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "C:\ProgramData\miniconda3\Scripts\conda.exe" set CONDA_EXE=C:\ProgramData\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "C:\miniconda3\Scripts\conda.exe" set CONDA_EXE=C:\miniconda3\Scripts\conda.exe
if not defined CONDA_EXE if exist "%USERPROFILE%\miniconda3\condabin\conda.bat" set CONDA_EXE=%USERPROFILE%\miniconda3\condabin\conda.bat
if not defined CONDA_EXE if exist "%USERPROFILE%\Miniconda3\condabin\conda.bat" set CONDA_EXE=%USERPROFILE%\Miniconda3\condabin\conda.bat

if not defined CONDA_EXE (
    where conda >nul 2>&1
    if %errorlevel% equ 0 set CONDA_EXE=conda
)

if not defined CONDA_EXE (
    echo Miniconda NOT found
    echo Install: https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe
    echo Then double-click CYBERTRADE.bat again
    pause
    exit /b 1
)

echo Found Miniconda: %CONDA_EXE%
call "%CONDA_EXE%" --version

echo Checking for env %ENV_NAME%...

set ENV_PYTHON=

if exist "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\miniconda3\envs\%ENV_NAME%\python.exe

if not defined ENV_PYTHON goto :create_env
goto :env_exists

:create_env
echo Env %ENV_NAME% not found - creating first time 1-2 min
echo Running conda env create -f %ENV_FILE%
call "%CONDA_EXE%" env create -f "%ENV_FILE%"
if %errorlevel% equ 0 goto :created_ok
echo Failed, trying with --force
call "%CONDA_EXE%" env create -f "%ENV_FILE%" --force
if %errorlevel% equ 0 goto :created_ok
echo Still failed. Try in Anaconda Prompt:
echo   conda clean --all
echo   conda env create -f %ENV_FILE% --force
pause
exit /b 1

:created_ok
echo OK Env %ENV_NAME% created
if exist "%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%USERPROFILE%\Miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=%LOCALAPPDATA%\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\ProgramData\miniconda3\envs\%ENV_NAME%\python.exe
if not defined ENV_PYTHON if exist "C:\miniconda3\envs\%ENV_NAME%\python.exe" set ENV_PYTHON=C:\miniconda3\envs\%ENV_NAME%\python.exe
goto :after_env_check

:env_exists
echo Env %ENV_NAME% exists at %ENV_PYTHON%

:after_env_check
if not defined ENV_PYTHON (
    echo Env python not found after creation
    pause
    exit /b 1
)

echo Env python: %ENV_PYTHON%

for %%F in ("%ENV_PYTHON%") do set ENV_ROOT=%%~dpF
if "%ENV_ROOT:~-1%"=="\" set ENV_ROOT=%ENV_ROOT:~0,-1%

echo Env root: %ENV_ROOT%

set PATH=%ENV_ROOT%;%ENV_ROOT%\Scripts;%ENV_ROOT%\Library\bin;%PATH%
set CONDA_DEFAULT_ENV=%ENV_NAME%
set CONDA_PREFIX=%ENV_ROOT%

echo Activated env %ENV_NAME% via PATH
python --version
python -m cybertrade doctor

echo.
echo ========================================================
echo  OK Starting CYBERTRADE web terminal
echo  Browser will open at http://localhost:8899
echo  Press Ctrl+C to stop
echo ========================================================
echo.

python -m cybertrade web --port 8899 --auto

pause
endlocal
