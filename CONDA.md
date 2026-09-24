# CYBERTRADE — Conda Env `cybertrade` (Windows Focused)

This project is now conda-ready with environment **`cybertrade`** — optimized for **Windows 10/11**.

## Windows Quick Start (Recommended)

### 1. Install Miniforge (Windows)

Miniforge is the open-source, conda-forge edition (recommended over Anaconda).

1. Download: **https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe**
2. Run installer — check **"Add Miniforge3 to PATH environment variable"** (or leave unchecked and use Miniforge Prompt)
3. After install, open **Miniforge Prompt** from Start Menu (search "Miniforge Prompt")

Verify:
```bat
conda --version
conda config --show channels
```

Alternative: Miniconda https://docs.anaconda.com/miniconda/install/#quick-command-line-install

### 2. Get CYBERTRADE

```bat
REM via git (in Miniforge Prompt)
git clone https://github.com/lorenare0684-ai/CYBERTRADE.git
cd CYBERTRADE

REM or download ZIP from GitHub and extract, then cd into folder
```

### 3. Create the env

**Option A — Double-click batch file (easiest):**
- Double-click `setup_conda.bat` in Explorer
- It will detect conda/mamba/micromamba and create env `cybertrade`

**Option B — Command Prompt / Miniforge Prompt:**
```bat
cd /d C:\path\to\CYBERTRADE
conda env create -f environment.yml
```

**Option C — PowerShell:**
```powershell
cd C:\path\to\CYBERTRADE
.\setup_conda.ps1
# If execution policy blocks: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

What it does:
- Creates env `cybertrade` with Python 3.11
- Installs `tk` (tkinter for desktop GUI `run_gui.py` / `run_gui.bat`)
- Installs `pytest>=7`
- Installs CYBERTRADE in editable mode (`pip -e .[dev]`) — runtime is 100% stdlib

### 4. Activate & Verify (Windows)

**In Miniforge Prompt / CMD:**
```bat
conda activate cybertrade

python -m cybertrade doctor
python -m cybertrade strategies
python -m cybertrade scenarios
python -m cybertrade backtest --bars 200
python -m unittest discover -s tests
```

**Or use helper:**
```bat
call activate_cybertrade.bat
```

**Browser HUD (recommended on Windows — no tk needed):**
```bat
run_web.bat
REM or: python -m cybertrade web --port 8899 --auto
REM Opens http://localhost:8899 with cyberpunk HUD
```

**Desktop HUD:**
```bat
run_gui.bat
REM or: python run_gui.py
```

### 5. Daily Usage (Windows)

```bat
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

REM Dry-run (live quotes, paper fills, zero venue orders)
python -m cybertrade run --dry-run --live

REM Deactivate
conda deactivate

REM Remove env
conda env remove -n cybertrade
```

## Linux / macOS (same environment.yml)

```bash
conda env create -f environment.yml
conda activate cybertrade
python -m cybertrade doctor
python -m cybertrade web --port 8899 --auto
```

## Files Added for Windows

| File | Purpose |
|------|---------|
| `environment.yml` | Cross-platform env, works on Windows/Linux/macOS |
| `environment-dev.yml` | Dev env with black, ruff, mypy, pytest-cov |
| `setup_conda.bat` | Windows CMD one-click env creation |
| `setup_conda.ps1` | PowerShell env creation |
| `activate_cybertrade.bat` | Windows activation helper (checks common install paths) |
| `run_gui.bat` | Launch desktop GUI on Windows |
| `run_web.bat` | Launch browser HUD on Windows |
| `setup_conda.sh` | Linux/macOS helper (still included) |
| `activate_cybertrade.sh` | Linux/macOS activation helper |

## `environment.yml` (Windows)

```yaml
name: cybertrade
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11
  - pip>=23
  - tk=8.6
  - pytest>=7
  - pip:
    - -e .[dev]
```

No extra Windows packages needed — project is 100% stdlib. `tk` gives you tkinter on Windows (usually already included).

## Troubleshooting Windows

| Issue | Fix |
|-------|-----|
| `conda` not recognized | Use **Miniforge Prompt** from Start Menu, not normal CMD. Or add Miniforge to PATH during install |
| `conda env create` fails | `conda clean --all` then retry. Ensure you are in CYBERTRADE folder with `environment.yml` |
| `tkinter unavailable` in `doctor` | `conda install -n cybertrade tk` or reinstall Miniforge. On Windows Python, tk is usually bundled — try `python -m tkinter` |
| PowerShell blocks `.ps1` | Run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` then `.\setup_conda.ps1` |
| Port 8899 busy | `python -m cybertrade web --port 0 --auto` or close other app using port |
| `pytest` missing | `conda activate cybertrade` then `conda install pytest` or `pip install pytest` |
| Antivirus blocks Miniforge installer | Temporarily disable real-time protection or allow installer |

## Why conda on Windows?

- **No admin needed for Python**: Miniforge installs in `%USERPROFILE%\miniforge3`
- **tkinter**: Windows Python from python.org includes tk, but conda ensures consistent version
- **Isolation**: Keeps CYBERTRADE separate from system Python
- **Reproducible**: `python=3.11` locked, same file works on Linux/macOS/Windows
- **No Visual C++ Build Tools needed**: All deps are pure Python or conda binaries (project has zero compiled runtime deps)

## Alternative: venv on Windows (if you don't want conda)

```bat
REM In CMD
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
pip install -e .[dev]
python -m cybertrade doctor
```

But conda is recommended for Windows because it handles `tk` and long path issues better.
