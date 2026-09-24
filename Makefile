.PHONY: conda-create conda-update conda-remove doctor test web gui backtest journal

# Miniconda only — normal Miniconda, no Miniforge/mamba/micromamba
ENV_NAME=cybertrade
ENV_FILE=environment.yml

conda-create:
	conda env create -f $(ENV_FILE)

conda-update:
	conda env update -n $(ENV_NAME) -f $(ENV_FILE) --prune

conda-remove:
	conda env remove -n $(ENV_NAME)

doctor:
	python -m cybertrade doctor

test:
	python -m unittest discover -s tests

web:
	python -m cybertrade web --port 8899 --auto

gui:
	python run_gui.py

backtest:
	python -m cybertrade backtest --bars 200

journal:
	python -m cybertrade journal
