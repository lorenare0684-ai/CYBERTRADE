# CYBERTRADE // NEON PROTOCOL — Windows PowerShell conda setup
# Creates conda env named cybertrade on Windows
# Usage (PowerShell):
#   .\setup_conda.ps1
#   conda activate cybertrade
#   python -m cybertrade doctor

$EnvName = "cybertrade"
$EnvFile = "environment.yml"

Write-Host "[*] CYBERTRADE conda setup for Windows (PowerShell)" -ForegroundColor Cyan
Write-Host "[*] Checking for conda..."

$CondaBin = $null
if (Get-Command conda -ErrorAction SilentlyContinue) { $CondaBin = "conda" }
elseif (Get-Command mamba -ErrorAction SilentlyContinue) { $CondaBin = "mamba" }
elseif (Get-Command micromamba -ErrorAction SilentlyContinue) { $CondaBin = "micromamba" }

if (-not $CondaBin) {
    Write-Host "[!] conda / mamba / micromamba not found in PATH" -ForegroundColor Red
    Write-Host "    Install Miniforge for Windows:"
    Write-Host "    https://github.com/conda-forge/miniforge"
    Write-Host "    Download Miniforge3-Windows-x86_64.exe and run installer"
    Write-Host "    Then open 'Miniforge Prompt' or PowerShell and re-run:"
    Write-Host "    .\setup_conda.ps1"
    Write-Host ""
    Write-Host "    Or install Miniconda:"
    Write-Host "    https://docs.anaconda.com/miniconda/install/"
    exit 1
}

Write-Host "[*] Using $CondaBin : $(& $CondaBin --version)" -ForegroundColor Green

if (-not (Test-Path $EnvFile)) {
    Write-Host "[!] $EnvFile not found in $(Get-Location)" -ForegroundColor Red
    exit 1
}

# Check if env exists
$envExists = & $CondaBin env list | Select-String -Pattern "^\s*$EnvName\s"
if ($envExists) {
    Write-Host "[*] Env '$EnvName' exists — updating from $EnvFile" -ForegroundColor Yellow
    & $CondaBin env update -n $EnvName -f $EnvFile --prune
} else {
    Write-Host "[*] Creating env '$EnvName' from $EnvFile" -ForegroundColor Yellow
    & $CondaBin env create -f $EnvFile
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Failed to create/update env" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "[OK] Conda env '$EnvName' ready." -ForegroundColor Green
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
