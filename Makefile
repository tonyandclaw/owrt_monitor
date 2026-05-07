.PHONY: install-dev test lint format dry-run

PYTHON ?= python3

install-dev:
	$(PYTHON) -m pip install -e ".[dev,serial]"

test:
	PYTHONPATH=python $(PYTHON) -m pytest

lint:
	PYTHONPATH=python $(PYTHON) -m ruff check python tests

format:
	PYTHONPATH=python $(PYTHON) -m ruff format python tests

dry-run:
	PYTHONPATH=python $(PYTHON) -m owrt_monitor dry-run --config configs/example.yaml

