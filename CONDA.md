# CYBERTRADE — Conda Env `cybertrade` (Normal Miniconda Only)

This project uses **normal Miniconda** — no Miniforge, no mamba, no micromamba. Just official Miniconda from Anaconda.

## Windows Quick Start (Miniconda Only)

### 1. Install Miniconda (Windows)

Official docs: **https://docs.anaconda.com/miniconda/install/**

1. Download: **https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe**
2. Run installer:
   - **Install for Just Me** (goes to `%USERPROFILE%\miniconda3`, no admin needed)
   - Leave "Add Miniconda3 to PATH" **unchecked** (recommended) and use **Anaconda Prompt** from Start Menu
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
```

### 3. Create the env (Miniconda only)

**Option A — Anaconda Prompt (RECOMMENDED, always works):**
```bat
cd /d C:\path\to\CYBERTRADE
conda env create -f environment.yml
```

**Option B — Double-click (fixed, auto-searches Miniconda):**
- In Explorer, double-click `setup_conda.bat`
- New version auto-searches Miniconda in:
  `%USERPROFILE%\miniconda3\Scripts\conda.exe`
  `%USERPROFILE%\Miniconda3\Scripts\conda.exe`
  etc., even if conda not in PATH

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

## FIX FOR: [!] conda not found in PATH

If you see this error when double-clicking `setup_conda.bat` in normal CMD:
```
[!] conda not found in PATH
    Install Miniconda for Windows:
    https://docs.anaconda.com/miniconda/install/
```

**This is NORMAL Miniconda behavior** — Miniconda does NOT add itself to PATH in normal CMD by default. Anaconda Prompt has conda in PATH automatically.

### FIX 1 (Recommended, 10 seconds):
1. Press Windows key, type **"Anaconda Prompt"**, open it
2. In Anaconda Prompt:
```bat
cd /d C:\path\to\CYBERTRADE
conda env create -f environment.yml
conda activate cybertrade
python -m cybertrade doctor
```

### FIX 2: Use fixed setup_conda.bat (now auto-searches):
- New `setup_conda.bat` searches common Miniconda locations even if not in PATH:
  - `%USERPROFILE%\miniconda3\Scripts\conda.exe`
  - `%USERPROFILE%\Miniconda3\Scripts\conda.exe`
  - `%LOCALAPPDATA%\miniconda3\Scripts\conda.exe`
  - `C:\ProgramData\miniconda3\Scripts\conda.exe`
  - `%USERPROFILE%\miniconda3\condabin\conda.bat`
- Just double-click `setup_conda.bat` again — it should now find conda

### FIX 3: Run fix_conda_path.bat
- Double-click `fix_conda_path.bat`
- It finds Miniconda and adds to PATH for current session
- Then run `setup_conda.bat`

### FIX 4: Init conda for future CMD/PowerShell
- Open **Anaconda Prompt**
- Run:
```bat
conda init cmd.exe
conda init powershell
```
- Close and reopen CMD/PowerShell — `conda` will now work there too

### FIX 5: Manually add to PATH
Add these 3 folders to System Environment Variables → PATH:
```
C:\Users\YourName\miniconda3
C:\Users\YourName\miniconda3\Scripts
C:\Users\YourName\miniconda3\condabin
```
Then restart CMD.

## Daily Usage (Windows, Miniconda)

```bat
REM Open Anaconda Prompt
conda activate cybertrade

REM Paper trading (default, safe)
python -m cybertrade run

REM Bounded crash recovery — PAPER ONLY
python -m cybertrade supervise --max-restarts 5 --restart-window 600

REM All-weather gauntlet
python -m cybertrade backtest --bars 600

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
curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o miniconda.sh
bash miniconda.sh -b -p $HOME/miniconda3
source $HOME/miniconda3/bin/activate

conda env create -f environment.yml
conda activate cybertrade
python -m cybertrade doctor
```

Or:
```bash
chmod +x setup_conda.sh
./setup_conda.sh
```

## Files (Miniconda Only)

| File | Purpose |
|------|---------|
| `environment.yml` | Main env — `name: cybertrade`, `python=3.11`, `tk`, `pytest`, `pip -e .[dev]` (Miniconda) |
| `environment-dev.yml` | Dev env with `black`, `ruff`, `mypy`, `pytest-cov` |
| `setup_conda.bat` | Windows CMD — **fixed** to auto-search Miniconda even if not in PATH |
| `setup_conda.ps1` | PowerShell — fixed to auto-search Miniconda |
| `setup_conda.sh` | Linux/macOS/Git Bash — Miniconda only |
| `fix_conda_path.bat` | **NEW** — Fixes "conda not found" by finding Miniconda and adding to PATH |
| `activate_cybertrade.bat` | Windows activation — checks Miniconda paths only |
| `activate_cybertrade.ps1` | PowerShell activation — Miniconda only |
| `run_gui.bat` / `run_web.bat` | Launch GUIs (Miniconda env) |
| `WINDOWS_SETUP.txt` | Plain-text Windows guide with PATH fix |

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

No Miniforge, no mamba — just normal Miniconda.

## Troubleshooting (Miniconda on Windows)

| Issue | Fix (Miniconda) |
|-------|-----------------|
| `[!] conda not found in PATH` in normal CMD | **Use Anaconda Prompt** from Start Menu (conda is always in PATH there). Or run fixed `setup_conda.bat` which now auto-searches. Or run `fix_conda_path.bat` |
| `conda` not recognized in normal CMD | Normal Miniconda behavior — use Anaconda Prompt. Or `conda init cmd.exe` then restart CMD |
| PowerShell blocks `.ps1` | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` then `.\setup_conda.ps1` |
| `conda env create` fails | `conda clean --all` then `conda env create -f environment.yml --force` |
| `tkinter unavailable` | `conda install -n cybertrade tk` |
| Port 8899 busy | `python -m cybertrade web --port 0 --auto` |
| Want to add conda to normal CMD permanently | In Anaconda Prompt: `conda init cmd.exe` + `conda init powershell`, restart shell |

## Why Miniconda?

- **Official**: From Anaconda (https://docs.anaconda.com/miniconda/)
- **No admin needed**: Installs to `%USERPROFILE%\miniconda3`
- **Anaconda Prompt**: Solves PATH issues — conda always works there
- **Isolation**: Keeps CYBERTRADE separate from system Python
- **Zero compiled runtime deps**: Project is 100% stdlib + `tk` + `pytest`
