# CYBERTRADE // NEON PROTOCOL — Miniconda setup (Windows PowerShell) — FIXED for PATH
# Creates conda env named cybertrade using Miniconda only
# Works even if conda not in PATH — auto-detects Miniconda in common locations
# Usage (PowerShell):
#   .\setup_conda.ps1
#   conda activate cybertrade
#   python -m cybertrade doctor

$EnvName = "cybertrade"
$EnvFile = "environment.yml"

Write-Host "[*] CYBERTRADE Miniconda setup for Windows (PowerShell)" -ForegroundColor Cyan
Write-Host "[*] Checking for conda (Miniconda)..."

$CondaCmd = $null

# Try conda in PATH first
if (Get-Command conda -ErrorAction SilentlyContinue) {
    $CondaCmd = "conda"
} else {
    Write-Host "[*] conda not in PATH — searching common Miniconda locations..." -ForegroundColor Yellow
    $possiblePaths = @(
        "$env:USERPROFILE\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\Miniconda3\Scripts\conda.exe",
        "$env:LOCALAPPDATA\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\anaconda3\Scripts\conda.exe",
        "C:\ProgramData\miniconda3\Scripts\conda.exe",
        "$env:USERPROFILE\miniconda3\condabin\conda.bat",
        "$env:USERPROFILE\Miniconda3\condabin\conda.bat",
        "C:\miniconda3\Scripts\conda.exe",
        "C:\Miniconda3\Scripts\conda.exe"
    )
    foreach ($p in $possiblePaths) {
        if (Test-Path $p) {
            Write-Host "[*] Found conda at $p" -ForegroundColor Green
            $CondaCmd = $p
            # Add its dir to PATH for future conda activate
            $condaDir = Split-Path $p -Parent
            $env:PATH = "$condaDir;$env:PATH"
            $env:PATH = "$env:USERPROFILE\miniconda3\Scripts;$env:USERPROFILE\miniconda3\condabin;$env:USERPROFILE\Miniconda3\Scripts;$env:USERPROFILE\Miniconda3\condabin;$env:PATH"
            break
        }
    }
}

if (-not $CondaCmd) {
    Write-Host "" 
    Write-Host "[!] conda NOT found — even after searching common locations" -ForegroundColor Red
    Write-Host ""
    Write-Host "    Searched:"
    Write-Host "      $env:USERPROFILE\miniconda3\Scripts\conda.exe"
    Write-Host "      $env:USERPROFILE\Miniconda3\Scripts\conda.exe"
    Write-Host "      $env:LOCALAPPDATA\miniconda3\Scripts\conda.exe"
    Write-Host "      C:\ProgramData\miniconda3\Scripts\conda.exe"
    Write-Host ""
    Write-Host "    FIX 1 (Recommended): Open Anaconda Prompt from Start Menu" -ForegroundColor Yellow
    Write-Host "      - Press Windows key, type 'Anaconda Prompt', open it"
    Write-Host "      - cd to this folder: cd /d `"$((Get-Location).Path)`""
    Write-Host "      - Then run: conda env create -f environment.yml"
    Write-Host ""
    Write-Host "    FIX 2: Add Miniconda to PATH" -ForegroundColor Yellow
    Write-Host "      - During Miniconda install, check 'Add Miniconda3 to PATH'"
    Write-Host "      - Or manually add to PATH:"
    Write-Host "        $env:USERPROFILE\miniconda3"
    Write-Host "        $env:USERPROFILE\miniconda3\Scripts"
    Write-Host "        $env:USERPROFILE\miniconda3\condabin"
    Write-Host ""
    Write-Host "    FIX 3: Install Miniconda if not installed" -ForegroundColor Yellow
    Write-Host "      https://docs.anaconda.com/miniconda/install/"
    Write-Host "      Download: Miniconda3-latest-Windows-x86_64.exe"
    Write-Host ""
    exit 1
}

Write-Host "[*] Using conda: $CondaCmd : $(& $CondaCmd --version)" -ForegroundColor Green

if (-not (Test-Path $EnvFile)) {
    Write-Host "[!] $EnvFile not found in $(Get-Location)" -ForegroundColor Red
    exit 1
}

# Check if env exists
$envExists = & $CondaCmd env list | Select-String -Pattern "^\s*$EnvName\s"
if ($envExists) {
    Write-Host "[*] Env '$EnvName' exists — updating from $EnvFile" -ForegroundColor Yellow
    & $CondaCmd env update -n $EnvName -f $EnvFile --prune
} else {
    Write-Host "[*] Creating env '$EnvName' from $EnvFile" -ForegroundColor Yellow
    & $CondaCmd env create -f $EnvFile
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Failed to create/update env" -ForegroundColor Red
    Write-Host "    Try: conda clean --all"
    Write-Host "    Then: conda env create -f $EnvFile --force"
    exit 1
}

Write-Host ""
Write-Host "[OK] Conda env '$EnvName' ready (Miniconda)." -ForegroundColor Green
Write-Host ""
Write-Host "    To activate (in Anaconda Prompt):"
Write-Host "      conda activate $EnvName"
Write-Host ""
Write-Host "    Then verify:"
Write-Host "      python -m cybertrade doctor"
Write-Host "      python -m unittest discover -s tests"
Write-Host "      python -m cybertrade web --port 8899 --auto"
Write-Host "      python run_gui.py"
Write-Host ""
Write-Host "    If conda activate says 'not recognized' in normal PowerShell:"
Write-Host "      Always use Anaconda Prompt from Start Menu"
Write-Host "      Or run: conda init powershell ; then restart PowerShell"
Write-Host ""
Write-Host "    Deactivate:"
Write-Host "      conda deactivate"
Write-Host ""
Write-Host "    Remove:"
Write-Host "      conda env remove -n $EnvName"
