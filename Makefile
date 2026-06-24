.PHONY: install-dev test test-go test-go-cover build-go lint format dry-run

PYTHON ?= python3
# Strict bar for the mature daemon/CLI (cmd/owrtd, cmd/owrtctl).
GO_COVER_MIN ?= 85.0
# Realistic bar for the newer standalone Go engine packages (internal/*,
# cmd/owrt-engine). The exec/hardware-coupled packages below cannot be unit
# tested without docker/a DUT and are exempted from the gate entirely.
GO_ENGINE_COVER_MIN ?= 70.0

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
	awk -v strict="$(GO_COVER_MIN)" -v engine="$(GO_ENGINE_COVER_MIN)" '\
		/coverage:/ { \
			pkg = $$2; pct = ""; \
			for (i = 1; i <= NF; i++) { \
				if ($$i ~ /%$$/) { pct = $$i; break } \
			} \
			sub(/%/, "", pct); \
			if (pkg ~ /internal\/(docker|serial|dut)$$/) next; \
			min = engine; \
			if (pkg ~ /cmd\/(owrtd|owrtctl)$$/) min = strict; \
			if (pct + 0 < min) { \
				printf "%s coverage %.1f%% below %.1f%%\n", pkg, pct, min; \
				fail = 1; \
			} \
		} \
		END { exit fail }' "$$out"

build-go:
	go build ./cmd/owrtd ./cmd/owrtctl ./cmd/owrt-engine

lint:
	PYTHONPATH=python $(PYTHON) -m ruff check python tests

format:
	PYTHONPATH=python $(PYTHON) -m ruff format python tests

dry-run:
	PYTHONPATH=python $(PYTHON) -m owrt_monitor dry-run --config configs/example.yaml
