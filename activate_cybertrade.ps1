# Activate cybertrade conda env on Windows (Miniconda only)
# Usage: .\activate_cybertrade.ps1
# Or: . .\activate_cybertrade.ps1  (dot-source to keep env in current shell)

$EnvName = "cybertrade"

Write-Host "[*] Activating conda env $EnvName (Miniconda)..." -ForegroundColor Cyan

# Try conda activate (Miniconda)
if (Get-Command conda -ErrorAction SilentlyContinue) {
    try {
        conda activate $EnvName
        Write-Host "[OK] Activated via Miniconda" -ForegroundColor Green
        python --version
        python -m cybertrade doctor
        exit 0
    } catch {
        Write-Host "[*] conda activate failed, trying PATH fallback" -ForegroundColor Yellow
    }
}

# Fallback: common Miniconda install locations
$possiblePaths = @(
    "$env:USERPROFILE\miniconda3\envs\$EnvName",
    "$env:USERPROFILE\Miniconda3\envs\$EnvName",
    "$env:LOCALAPPDATA\miniconda3\envs\$EnvName",
    "C:\ProgramData\miniconda3\envs\$EnvName",
    "$env:USERPROFILE\anaconda3\envs\$EnvName"
)

foreach ($p in $possiblePaths) {
    if (Test-Path "$p\python.exe") {
        Write-Host "[*] Found Miniconda env at $p" -ForegroundColor Yellow
        $env:PATH = "$p;$p\Scripts;$env:PATH"
        $env:CONDA_DEFAULT_ENV = $EnvName
        $env:CONDA_PREFIX = $p
        python --version
        python -m cybertrade doctor
        exit 0
    }
}

Write-Host "[!] Conda env $EnvName not found (Miniconda)" -ForegroundColor Red
Write-Host "    Create it with:"
Write-Host "      conda env create -f environment.yml"
Write-Host "    Or run:"
Write-Host "      .\setup_conda.ps1"
Write-Host ""
Write-Host "    Install Miniconda if not installed:"
Write-Host "      https://docs.anaconda.com/miniconda/install/"
exit 1
