# Repository Guidelines

## Project Structure & Module Organization

`owrt_monitor` is a Python + Go project for coordinating OpenWrt builds, firmware artifacts, DUT flashing, and post-upgrade tests. Python source lives in `python/owrt_monitor/` and provides the `owrt-monitor` CLI plus workflow, config, serial, transfer, analysis, and reporting modules. Go commands live in `cmd/owrtd/` and `cmd/owrtctl/`. Tests are under `tests/python/`, with fixtures in `tests/fixtures/`. Example configuration is in `configs/example.yaml`; user docs are in `docs/`. Read `ARCHITECTURE.md`, `TODO.md`, and `CLAUDE.md` before changing workflow boundaries.

## Build, Test, and Development Commands

- `make install-dev`: install the package editable with dev and serial extras.
- `make test`: run the Python pytest suite with `PYTHONPATH=python`.
- `make lint`: run Ruff checks over `python` and `tests`.
- `make format`: format Python files with Ruff.
- `make dry-run`: run `owrt_monitor` against `configs/example.yaml` without flashing.
- `go test ./...`: run Go tests.
- `go build ./cmd/owrtd ./cmd/owrtctl`: build Go command binaries.

For targeted pytest runs, use `PYTHONPATH=python python3 -m pytest tests/python/test_workflow.py::test_name`.

## Coding Style & Naming Conventions

Python targets 3.11+. Ruff enforces `E`, `F`, `I`, `UP`, and `B` rules with a 100-character line length. Use 4-space indentation, snake_case for functions and modules, PascalCase for classes, and typed Pydantic models where already used. Prefer argument lists over shell strings for subprocess and Docker calls. Keep workflow state, events, SQLite records, and reports consistent.

Go targets Go 1.22. Use standard `gofmt` formatting and idiomatic package-local names.

## Testing Guidelines

Python tests use pytest and should be named `test_*.py` under `tests/python/`. Add focused unit tests for config, parsing, and state behavior, and integration-style tests for workflow changes using the existing fake Docker and fixture patterns. Keep destructive DUT behavior behind explicit flags such as `--allow-flash`; prefer `dry-run` coverage for safe paths.

## Commit & Pull Request Guidelines

Recent commits use concise imperative or scope-prefixed subjects, for example `owrtd: GET /v1/jobs/{id}/files/<path> serves run-dir contents` or `Add custom-script tests: host-side subprocess tests with DUT env vars`. Keep subjects specific and under control. PRs should describe behavior changes, list tests run, link related issues or roadmap items, and include screenshots or logs when UI, reports, or daemon responses change.

## Security & Configuration Tips

Do not commit real DUT credentials, secrets, generated artifacts, or local state databases. Use `OwrtConfig.redacted_dump` for logs, reports, and snapshots. Treat `run --allow-flash` and `flash --allow-flash` as destructive operations; validate with `make dry-run` first.
