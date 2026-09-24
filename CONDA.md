# CYBERTRADE — Conda Env `cybertrade` (Normal Miniconda Only) — ONE CLICK

## ONE-CLICK START (Windows) — Just double-click, no Anaconda Prompt needed

**You said you don't want Anaconda Prompt — just a click. Here it is:**

1. Install Miniconda **once** (if not installed):
   - Download: **https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe**
   - Run installer → **Just Me** → keep defaults (no need to check Add to PATH)

2. **Double-click `CYBERTRADE.bat`** in Explorer — that's it.

What it does automatically (no typing):
- Searches Miniconda in 9 common locations even if `conda` not in PATH
- If env `cybertrade` doesn't exist, creates it from `environment.yml` (first time 1-2 min)
- Activates env by setting PATH directly (no `conda activate` needed, so no Anaconda Prompt needed)
- Runs `python -m cybertrade doctor` (10/10 checks)
- Runs `python -m cybertrade web --port 8899 --auto` and opens browser at http://localhost:8899

Other one-click files:
- `CYBERTRADE_GUI.bat` → same but launches desktop GUI (`run_gui.py`)
- `click_to_run.bat` → ultra simple name, same as `CYBERTRADE.bat`

**If double-click flashes and closes:** Right-click → Edit, see error, or open CMD and run `CYBERTRADE.bat` to see log. If says "Miniconda NOT found", install Miniconda from link above.

---

## Normal Miniconda Setup (if you prefer typing)

### 1. Install Miniconda (Windows)

Official docs: **https://docs.anaconda.com/miniconda/install/**

1. Download: **https://repo.anaconda.com/miniconda/Miniconda3-latest-Windows-x86_64.exe**
2. Run installer → **Just Me** (`%USERPROFILE%\miniconda3`)
3. Open **Anaconda Prompt** from Start Menu (search "Anaconda Prompt")

Verify:
```bat
conda --version
```

### 2. Get CYBERTRADE

```bat
REM In Anaconda Prompt
cd /d C:\Users\YourName\Downloads
git clone https://github.com/lorenare0684-ai/CYBERTRADE.git
cd CYBERTRADE
```

### 3. Create env

```bat
REM In Anaconda Prompt (recommended)
conda env create -f environment.yml

REM Or double-click setup_conda.bat (now auto-searches Miniconda even if not in PATH)
```

### 4. Activate & Verify

```bat
conda activate cybertrade
python -m cybertrade doctor
python -m unittest discover -s tests
```

### 5. Run

```bat
run_web.bat
REM or: python -m cybertrade web --port 8899 --auto

run_gui.bat
REM or: python run_gui.py
```

## FIX FOR: [!] conda not found in PATH

If you see this in normal CMD when running `setup_conda.bat`:
```
[!] conda not found in PATH
```

**This is NORMAL Miniconda** — it doesn't add to PATH in normal CMD. Anaconda Prompt has it.

**FIX 1 (10 sec, recommended):** Use Anaconda Prompt from Start Menu

**FIX 2:** Use new **one-click `CYBERTRADE.bat`** — it auto-searches Miniconda in common locations even if not in PATH, no Anaconda Prompt needed

**FIX 3:** Double-click `fix_conda_path.bat` — finds Miniconda and adds to PATH for current session

**FIX 4:** In Anaconda Prompt:
```bat
conda init cmd.exe
conda init powershell
```
Restart CMD/PowerShell — conda will work there too

## Files (Miniconda Only)

| File | Purpose |
|------|---------|
| `CYBERTRADE.bat` | **ONE-CLICK** — finds Miniconda, creates env if needed, runs web HUD, no Anaconda Prompt needed |
| `CYBERTRADE_GUI.bat` | ONE-CLICK GUI — same but desktop GUI |
| `click_to_run.bat` | ONE-CLICK — ultra simple name, calls CYBERTRADE.bat |
| `environment.yml` | Env def — `name: cybertrade`, `python=3.11`, `tk`, `pytest`, `pip -e .[dev]` |
| `environment-dev.yml` | Dev env with black, ruff, mypy |
| `setup_conda.bat` | Creates env (now auto-searches Miniconda even if not in PATH) |
| `setup_conda.ps1` | PowerShell — auto-searches Miniconda |
| `fix_conda_path.bat` | Fixes "conda not found" by finding Miniconda |
| `activate_cybertrade.bat` | Activation helper (Miniconda paths) |
| `run_gui.bat` / `run_web.bat` | Launch GUIs (Miniconda) |
| `ONE_CLICK.txt` | Plain-text one-click guide |
| `WINDOWS_SETUP.txt` | Full Windows guide with PATH fix |

## `environment.yml`

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

## Why Miniconda + One-Click?

- **Official Miniconda**: https://docs.anaconda.com/miniconda/
- **One-click**: `CYBERTRADE.bat` sets PATH directly to `miniconda3\envs\cybertrade\Scripts` — no `conda activate` needed, so no Anaconda Prompt needed
- **Auto-search**: Finds Miniconda in 9 locations even if not in PATH
- **Auto-create**: Creates env if missing
- **No admin**: Installs to `%USERPROFILE%\miniconda3`
