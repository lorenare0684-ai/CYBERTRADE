# CYBERTRADE // NEON PROTOCOL — Miniconda setup (Windows PowerShell)
# Creates conda env named cybertrade using Miniconda only
# Usage (PowerShell):
#   .\setup_conda.ps1
#   conda activate cybertrade
#   python -m cybertrade doctor

$EnvName = "cybertrade"
$EnvFile = "environment.yml"

Write-Host "[*] CYBERTRADE Miniconda setup for Windows (PowerShell)" -ForegroundColor Cyan
Write-Host "[*] Checking for conda (Miniconda)..."

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    Write-Host "[!] conda not found in PATH" -ForegroundColor Red
    Write-Host "    Install Miniconda for Windows:"
    Write-Host "    https://docs.anaconda.com/miniconda/install/"
    Write-Host "    Download Miniconda3-latest-Windows-x86_64.exe and run installer"
    Write-Host "    Then open 'Anaconda Prompt' or PowerShell and re-run:"
    Write-Host "    .\setup_conda.ps1"
    Write-Host ""
    Write-Host "    Tip: Use Anaconda Prompt from Start Menu - conda is already in PATH there"
    exit 1
}

Write-Host "[*] Using conda : $(conda --version)" -ForegroundColor Green

if (-not (Test-Path $EnvFile)) {
    Write-Host "[!] $EnvFile not found in $(Get-Location)" -ForegroundColor Red
    exit 1
}

# Check if env exists
$envExists = conda env list | Select-String -Pattern "^\s*$EnvName\s"
if ($envExists) {
    Write-Host "[*] Env '$EnvName' exists — updating from $EnvFile" -ForegroundColor Yellow
    conda env update -n $EnvName -f $EnvFile --prune
} else {
    Write-Host "[*] Creating env '$EnvName' from $EnvFile" -ForegroundColor Yellow
    conda env create -f $EnvFile
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Failed to create/update env" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[OK] Conda env '$EnvName' ready (Miniconda)." -ForegroundColor Green
Write-Host ""
Write-Host "    To activate:"
Write-Host "      conda activate $EnvName"
Write-Host ""
Write-Host "    Then verify:"
Write-Host "      python -m cybertrade doctor"
Write-Host "      python -m unittest discover -s tests"
Write-Host "      python -m cybertrade web --port 8899 --auto"
Write-Host "      python run_gui.py"
Write-Host ""
Write-Host "    Deactivate:"
Write-Host "      conda deactivate"
Write-Host ""
Write-Host "    Remove:"
Write-Host "      conda env remove -n $EnvName"
