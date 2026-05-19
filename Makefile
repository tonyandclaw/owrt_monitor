.PHONY: install-dev test test-go test-go-cover lint format dry-run

PYTHON ?= python3
GO_COVER_MIN ?= 85.0

install-dev:
	$(PYTHON) -m pip install -e ".[dev,serial]"

test:
	PYTHONPATH=python $(PYTHON) -m pytest

test-go:
	go test ./...

test-go-cover:
	@set -e; \
	out=$$(mktemp); \
	trap 'rm -f "$$out"' EXIT; \
	go test -count=1 -cover ./... | tee "$$out"; \
	awk -v min="$(GO_COVER_MIN)" '\
		/coverage:/ { \
			pct = ""; \
			for (i = 1; i <= NF; i++) { \
				if ($$i ~ /%$$/) { pct = $$i; break } \
			} \
			sub(/%/, "", pct); \
			if (pct + 0 < min) { \
				printf "%s coverage %.1f%% below %.1f%%\n", $$2, pct, min; \
				fail = 1; \
			} \
		} \
		END { exit fail }' "$$out"

lint:
	PYTHONPATH=python $(PYTHON) -m ruff check python tests

format:
	PYTHONPATH=python $(PYTHON) -m ruff format python tests

dry-run:
	PYTHONPATH=python $(PYTHON) -m owrt_monitor dry-run --config configs/example.yaml
