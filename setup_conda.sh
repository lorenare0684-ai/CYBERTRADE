#!/usr/bin/env bash
# CYBERTRADE // NEON PROTOCOL — Miniconda setup (Linux/macOS/Windows Git Bash)
# Creates and activates the `cybertrade` conda environment using Miniconda only
# Usage:
#   chmod +x setup_conda.sh
#   ./setup_conda.sh          # create env + install package
#   conda activate cybertrade # activate
#   python -m cybertrade doctor
#   python -m cybertrade web --port 8899 --auto
#
set -euo pipefail

ENV_NAME="cybertrade"
ENV_FILE="environment.yml"

# Detect conda (Miniconda only)
if ! command -v conda >/dev/null 2>&1; then
  echo "[!] conda not found (Miniconda required)."
  echo "    Install Miniconda:"
  echo "      https://docs.anaconda.com/miniconda/install/"
  echo ""
  echo "    Quick install (Linux x86_64):"
  echo "      curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o miniconda.sh"
  echo "      bash miniconda.sh -b -p \$HOME/miniconda3"
  echo "      source \$HOME/miniconda3/bin/activate"
  echo ""
  echo "    Windows: Download Miniconda3-latest-Windows-x86_64.exe from:"
  echo "      https://docs.anaconda.com/miniconda/install/"
  echo "      Then open Anaconda Prompt"
  echo ""
  echo "    Then re-run this script."
  exit 1
fi

echo "[*] Using conda (Miniconda): $(conda --version 2>&1)"

# Ensure environment.yml exists
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[!] $ENV_FILE not found in $(pwd)"
  exit 1
fi

# Create or update env
if conda env list | grep -qE "^\s*$ENV_NAME\s"; then
  echo "[*] Env '$ENV_NAME' exists — updating from $ENV_FILE"
  conda env update -n "$ENV_NAME" -f "$ENV_FILE" --prune
else
  echo "[*] Creating env '$ENV_NAME' from $ENV_FILE"
  conda env create -f "$ENV_FILE"
fi

echo ""
echo "[✓] Conda env '$ENV_NAME' ready (Miniconda)."
echo ""
echo "    To activate:"
echo "      conda activate $ENV_NAME"
echo ""
echo "    Then verify:"
echo "      python -m cybertrade doctor"
echo "      python -m unittest discover -s tests  # 593 tests"
echo "      python -m cybertrade web --port 8899 --auto   # browser HUD"
echo "      python run_gui.py                           # desktop HUD (needs tk)"
echo ""
echo "    Deactivate:"
echo "      conda deactivate"
echo ""
echo "    Remove:"
echo "      conda env remove -n $ENV_NAME"
