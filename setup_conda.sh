#!/usr/bin/env bash
# CYBERTRADE // NEON PROTOCOL — conda env setup
# Creates and activates the `cybertrade` conda environment
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
PYTHON_VERSION="3.11"

# Detect conda / mamba / micromamba
CONDA_BIN=""
if command -v conda >/dev/null 2>&1; then
  CONDA_BIN="conda"
elif command -v mamba >/dev/null 2>&1; then
  CONDA_BIN="mamba"
elif command -v micromamba >/dev/null 2>&1; then
  CONDA_BIN="micromamba"
else
  echo "[!] conda / mamba / micromamba not found."
  echo "    Install Miniforge (recommended) or Miniconda:"
  echo "      https://github.com/conda-forge/miniforge#install"
  echo "      https://docs.anaconda.com/miniconda/install/#quick-command-line-install"
  echo ""
  echo "    Quick install (Linux x86_64):"
  echo "      curl -L -O https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh"
  echo "      bash Miniforge3-Linux-x86_64.sh -b -p \$HOME/miniforge3"
  echo "      source \$HOME/miniforge3/bin/activate"
  echo ""
  echo "    Then re-run this script."
  exit 1
fi

echo "[*] Using $CONDA_BIN: $($CONDA_BIN --version 2>&1 || echo ok)"

# Ensure environment.yml exists
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[!] $ENV_FILE not found in $(pwd)"
  exit 1
fi

# Create or update env
if $CONDA_BIN env list | grep -qE "^\s*$ENV_NAME\s"; then
  echo "[*] Env '$ENV_NAME' exists — updating from $ENV_FILE"
  $CONDA_BIN env update -n "$ENV_NAME" -f "$ENV_FILE" --prune
else
  echo "[*] Creating env '$ENV_NAME' from $ENV_FILE"
  $CONDA_BIN env create -f "$ENV_FILE"
fi

echo ""
echo "[✓] Conda env '$ENV_NAME' ready."
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
