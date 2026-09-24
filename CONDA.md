# CYBERTRADE Conda Environment

This project is now conda-ready with an environment named **`cybertrade`**.

## Quick Start

### 1. Install conda (if you don't have it)

Recommended: **Miniforge** (conda-forge, open-source)

```bash
# Linux x86_64
curl -L -O https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh
bash Miniforge3-Linux-x86_64.sh -b -p $HOME/miniforge3
source $HOME/miniforge3/bin/activate

# macOS Apple Silicon
curl -L -O https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX-arm64.sh
bash Miniforge3-MacOSX-arm64.sh -b -p $HOME/miniforge3
source $HOME/miniforge3/bin/activate
```

Alternative: Miniconda https://docs.anaconda.com/miniconda/install/

### 2. Create the env

```bash
git clone https://github.com/lorenare0684-ai/CYBERTRADE.git
cd CYBERTRADE

# Option A — via helper script (detects conda/mamba/micromamba)
chmod +x setup_conda.sh
./setup_conda.sh

# Option B — direct conda
conda env create -f environment.yml
```

What it does:
- Creates env `cybertrade` with Python 3.11
- Installs `tk` (tkinter for desktop GUI)
- Installs `pytest>=7` for dev
- Installs this package in editable mode (`pip -e .[dev]`) — 100% stdlib runtime

### 3. Activate & Verify

```bash
conda activate cybertrade

python -m cybertrade doctor          # 10 environment checks
python -m cybertrade strategies      # list 40 strategies
python -m cybertrade scenarios       # list 10 stress scenarios
python -m cybertrade backtest --bars 200   # quick backtest
python -m unittest discover -s tests # 593 tests (full suite)

# Browser HUD (cyberpunk terminal)
python -m cybertrade web --port 8899 --auto

# Desktop HUD (needs tkinter)
python run_gui.py
```

### 4. Usage after activation

```bash
conda activate cybertrade

# Paper trading (default, safe)
python -m cybertrade run

# Bounded crash recovery — PAPER ONLY
python -m cybertrade supervise --max-restarts 5 --restart-window 600

# All-weather gauntlet
python -m cybertrade backtest --bars 600

# Walk-forward optimization
python -m cybertrade optimize --scenario regime_whipsaw

# Journal analytics
python -m cybertrade journal

# Dry-run (live quotes, paper fills, zero venue orders)
python -m cybertrade run --dry-run --live  # needs Quotex session (see docs)

# Deactivate
conda deactivate

# Remove
conda env remove -n cybertrade
```

## Environment File

`environment.yml`:

```yaml
name: cybertrade
channels:
  - conda-forge
  - defaults
dependencies:
  - python=3.11
  - pip
  - tk
  - pytest>=7
  - pip:
    - -e .[dev]
```

## Why conda?

- **Reproducible Python version**: locks to 3.11 (requires >=3.10)
- **tkinter**: `conda install tk` gives you tkinter without `apt install python3-tk` on many systems
- **Isolation**: keeps CYBERTRADE deps separate from system Python
- **Cross-platform**: same file works on Linux, macOS, Windows

## Notes

- Runtime is **100% Python standard library** — no third-party packages needed beyond `tk` for GUI and `pytest` for tests
- `data/` (journals, logs, configs with secrets) is gitignored — never committed
- `python -m cybertrade web` is the recommended entry point (no tk needed)
- Desktop GUI needs tkinter: if `python -m cybertrade doctor` reports `tkinter missing`, install `tk` in conda (`conda install -n cybertrade tk`) or system package `python3-tk`

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `conda: command not found` | Install Miniforge/Miniconda and `source ~/miniforge3/bin/activate` |
| `tkinter unavailable` | `conda install -n cybertrade tk` or `apt install python3-tk` |
| `pytest` missing | `conda activate cybertrade && conda install pytest` or `pip install pytest` |
| Port 8899 busy | `python -m cybertrade web --port 8899 --auto` picks free port, or specify `--port 0` |

## Alternative: venv (if you don't want conda)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
python -m cybertrade doctor
```

But conda is recommended for tkinter support.
