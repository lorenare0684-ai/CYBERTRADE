#!/usr/bin/env bash
# Activate the cybertrade conda env (Miniconda only)
# Usage: source ./activate_cybertrade.sh

ENV_NAME="cybertrade"

# Try real conda (Miniconda) first if env exists
if command -v conda >/dev/null 2>&1 && conda env list 2>/dev/null | grep -q "cybertrade"; then
  echo "[*] Activating conda env 'cybertrade' via Miniconda"
  # shellcheck disable=SC1091
  eval "$(conda shell.bash hook 2>/dev/null || echo '')"
  conda activate "$ENV_NAME" 2>/dev/null && {
    echo "[✓] Python: $(which python) ($(python --version 2>&1))"
    python -m cybertrade doctor
    return 0 2>/dev/null || exit 0
  }
fi

if [ -d "$HOME/miniconda3/envs/cybertrade/bin" ]; then
  echo "[*] Activating Miniconda env at ~/miniconda3/envs/cybertrade"
  export PATH="$HOME/miniconda3/envs/cybertrade/bin:$PATH"
  export VIRTUAL_ENV="$HOME/miniconda3/envs/cybertrade"
  export CONDA_DEFAULT_ENV="cybertrade"
  export CONDA_PREFIX="$HOME/miniconda3/envs/cybertrade"
  echo "[✓] Python: $(which python) ($(python --version 2>&1))"
  python -m cybertrade doctor
  return 0 2>/dev/null || exit 0
fi

if [ -d "$HOME/Miniconda3/envs/cybertrade/bin" ]; then
  echo "[*] Activating Miniconda env at ~/Miniconda3/envs/cybertrade"
  export PATH="$HOME/Miniconda3/envs/cybertrade/bin:$PATH"
  export VIRTUAL_ENV="$HOME/Miniconda3/envs/cybertrade"
  export CONDA_DEFAULT_ENV="cybertrade"
  export CONDA_PREFIX="$HOME/Miniconda3/envs/cybertrade"
  echo "[✓] Python: $(which python) ($(python --version 2>&1))"
  python -m cybertrade doctor
  return 0 2>/dev/null || exit 0
fi

if [ -d ".venv-cybertrade/bin" ]; then
  echo "[*] Activating local venv .venv-cybertrade (fallback)"
  # shellcheck disable=SC1091
  source .venv-cybertrade/bin/activate
  echo "[✓] Python: $(which python) ($(python --version 2>&1))"
  python -m cybertrade doctor
  return 0 2>/dev/null || exit 0
fi

echo "[!] No conda env 'cybertrade' found (Miniconda)."
echo "    Create it with:"
echo "      conda env create -f environment.yml"
echo "    or"
echo "      ./setup_conda.sh"
echo ""
echo "    Install Miniconda:"
echo "      https://docs.anaconda.com/miniconda/install/"
return 1 2>/dev/null || exit 1
