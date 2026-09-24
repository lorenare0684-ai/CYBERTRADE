# CYBERTRADE — Conda Env `cybertrade` (Normal Miniconda Only)

This project uses **normal Miniconda** — no Miniforge, no mamba, no micromamba. Just official Miniconda from Anaconda.

## Windows Quick Start (Miniconda Only)

### 1. Install Miniconda (Windows)

Official docs: **https://docs.anaconda.com/miniconda/install/**

1. Download: **https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe**
2. Run installer:
   - **Install for Just Me** (goes to `%USERPROFILE%\miniconda3`, no admin needed)
   - You can leave "Add Miniconda3 to PATH" **unchecked** (recommended) and use **Anaconda Prompt** from Start Menu
   - Or check it if you want `conda` in normal CMD/PowerShell
3. After install, open **Anaconda Prompt** from Start Menu (search "Anaconda Prompt")

Verify in Anaconda Prompt:
```bat
conda --version
REM Should show: conda 23.x.x or 24.x.x
```

### 2. Get CYBERTRADE

```bat
REM In Anaconda Prompt
cd /d C:\Users\YourName\Downloads
git clone https://github.com/lorenare0684-ai/CYBERTRADE.git
cd CYBERTRADE

REM Or download ZIP from GitHub and extract, then cd into folder
```

### 3. Create the env (Miniconda only)

**Option A — Double-click (easiest, Miniconda only):**
- In Explorer, double-click `setup_conda.bat`
- It uses only `conda` (Miniconda), no mamba/micromamba

**Option B — Anaconda Prompt (CMD):**
```bat
cd /d C:\path\to\CYBERTRADE
conda env create -f environment.yml
```

**Option C — PowerShell (Miniconda only):**
```powershell
cd C:\path\to\CYBERTRADE
.\setup_conda.ps1
# If blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

What `environment.yml` does (Miniconda):
- Creates env `cybertrade` with Python 3.11
- Installs `tk` (tkinter for desktop GUI)
- Installs `pytest>=7`
- Installs CYBERTRADE in editable mode (`pip -e .[dev]`) — runtime is 100% stdlib

### 4. Activate & Verify (Windows, Miniconda)

**In Anaconda Prompt:**
```bat
conda activate cybertrade

python -m cybertrade doctor
python -m cybertrade strategies
python -m cybertrade scenarios
python -m cybertrade backtest --bars 200
python -m unittest discover -s tests
```

**Or use helper (Miniconda only):**
```bat
call activate_cybertrade.bat
```

**Browser HUD (recommended on Windows):**
```bat
run_web.bat
REM or: python -m cybertrade web --port 8899 --auto
REM Opens http://localhost:8899
```

**Desktop HUD:**
```bat
run_gui.bat
REM or: python run_gui.py
```

### 5. Daily Usage (Windows, Miniconda)

```bat
REM Open Anaconda Prompt
conda activate cybertrade

REM Paper trading (default, safe)
python -m cybertrade run

REM Bounded crash recovery — PAPER ONLY
python -m cybertrade supervise --max-restarts 5 --restart-window 600

REM All-weather gauntlet
python -m cybertrade backtest --bars 600

REM Walk-forward optimization
python -m cybertrade optimize --scenario regime_whipsaw

REM Journal analytics
python -m cybertrade journal

REM Deactivate
conda deactivate

REM Remove env
conda env remove -n cybertrade
```

## Linux / macOS (Miniconda Only)

```bash
# Install Miniconda: https://docs.anaconda.com/miniconda/install/
# Linux x86_64:
curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o miniconda.sh
bash miniconda.sh -b -p $HOME/miniconda3
source $HOME/miniconda3/bin/activate

# Then:
conda env create -f environment.yml
conda activate cybertrade
python -m cybertrade doctor
python -m cybertrade web --port 8899 --auto
```

Or use:
```bash
chmod +x setup_conda.sh
./setup_conda.sh
```

## Files (Miniconda Only)

| File | Purpose |
|------|---------|
| `environment.yml` | Main env — `name: cybertrade`, `python=3.11`, `tk`, `pytest`, `pip -e .[dev]`, channels `defaults` + `conda-forge` (Miniconda) |
| `environment-dev.yml` | Dev env with `black`, `ruff`, `mypy`, `pytest-cov` |
| `setup_conda.bat` | Windows CMD — creates env using **only conda (Miniconda)** |
| `setup_conda.ps1` | PowerShell — Miniconda only |
| `setup_conda.sh` | Linux/macOS/Git Bash — Miniconda only |
| `activate_cybertrade.bat` | Windows activation — checks Miniconda paths only |
| `activate_cybertrade.ps1` | PowerShell activation — Miniconda only |
| `activate_cybertrade.sh` | Bash activation — Miniconda only |
| `run_gui.bat` | Launch desktop GUI (Miniconda env) |
| `run_web.bat` | Launch browser HUD (Miniconda env) |
| `WINDOWS_SETUP.txt` | Plain-text Windows guide (Miniconda) |

## `environment.yml` (Miniconda)

```yaml
name: cybertrade
channels:
  - defaults
  - conda-forge
dependencies:
  - python=3.11
  - pip>=23
  - tk=8.6
  - pytest>=7
  - pip:
    - -e .[dev]
```

No Miniforge, no mamba, no micromamba — just normal Miniconda.

## Troubleshooting (Miniconda on Windows)

| Issue | Fix (Miniconda) |
|-------|-----------------|
| `conda` not recognized | Use **Anaconda Prompt** from Start Menu, not normal CMD. Anaconda Prompt has conda in PATH automatically |
| `conda env create` fails | `conda clean --all` then `conda env create -f environment.yml --force` |
| `tkinter unavailable` | `conda install -n cybertrade tk` then `python -m tkinter` (should open test window) |
| PowerShell blocks `.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` then `.\setup_conda.ps1` |
| Port 8899 busy | `python -m cybertrade web --port 0 --auto` |
| Want to update env | `conda env update -f environment.yml --prune` |
| Need to reinstall Miniconda | Uninstall from Add/Remove Programs, delete `%USERPROFILE%\miniconda3`, reinstall exe |

## Why Miniconda?

- **Official**: From Anaconda, normal Miniconda (https://docs.anaconda.com/miniconda/)
- **No admin needed**: Installs to `%USERPROFILE%\miniconda3` for Just Me
- **Isolation**: Keeps CYBERTRADE separate from system Python
- **Reproducible**: `python=3.11` locked
- **Windows friendly**: Anaconda Prompt handles PATH, no need to edit environment variables
- **Zero compiled runtime deps**: Project is 100% stdlib + `tk` + `pytest` — pure Miniconda

## Alternative: venv on Windows (if you don't want conda at all)

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -e .[dev]
python -m cybertrade doctor
```

But Miniconda is recommended for Windows because Anaconda Prompt solves PATH issues.
