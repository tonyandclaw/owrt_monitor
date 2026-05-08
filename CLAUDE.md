# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

`owrt_monitor` is a Python+Go hybrid that orchestrates OpenWrt builds, firmware export, DUT flashing over USB serial, and post-upgrade smoke tests on a macOS host.

- Python (`python/owrt_monitor/`) is the active control plane and ships the user-facing `owrt-monitor` CLI. Everything in the MVP runs here.
- Go (`cmd/owrtd`, `cmd/owrtctl`, `internal/`) is reserved for a later runner/daemon milestone. `owrtd` currently exposes only `/healthz` and a `501` stub at `/v1/jobs`; `owrtctl` is a placeholder. `internal/{runner,locks,api,logs}` are empty `.gitkeep` directories. Do not invent runner/daemon features without coordinating with the roadmap in `TODO.md` and `ARCHITECTURE.md`.
- See `ARCHITECTURE.md` for the canonical workflow state machine, language split rationale, locking model, and persistence schema. `TODO.md` is the live roadmap. `docs/quickstart.md` and `docs/config-reference.md` document the CLI and YAML schema.

## Common commands

Python development uses `make` targets (which set `PYTHONPATH=python` for you):

```sh
make install-dev      # pip install -e ".[dev,serial]"
make lint             # ruff check python tests
make format           # ruff format python tests
make test             # pytest (testpaths = tests/python)
make dry-run          # owrt-monitor dry-run --config configs/example.yaml
```

Run a single Python test:

```sh
PYTHONPATH=python python3 -m pytest tests/python/test_workflow.py::test_name
```

Go (no tests yet, but CI runs this):

```sh
go test ./...
go build ./cmd/owrtd ./cmd/owrtctl
```

CLI entrypoint after `pip install -e`:

```sh
owrt-monitor {validate|dry-run|build|run|flash|test|status} --config configs/example.yaml
```

`run --allow-flash` and `flash --allow-flash` execute the destructive `sysupgrade` against the DUT — guard rails refuse to flash unless that flag is explicit, and `dry-run` is the safe preview.

## Architecture notes that aren't obvious from filenames

**Three workflow classes, one shared safety contract.** `workflow.py` exposes `BuildWorkflow`, `FlashWorkflow`, and `SmokeTestWorkflow`. Each one creates a fresh `job_<uuid12>` directory under `project.artifact_dir`, snapshots the (redacted) config, opens the SQLite `JobStore`, drives a `JobState` transition stream through `EventLogger` (writes to both `events.jsonl` and the `job_events` table), and calls `write_report` at the end. New workflows must follow this same shape so the `status` command, reports, and recovery story keep working.

**State transitions persist before side effects.** `_transition` writes to SQLite and emits a JSONL event before any external action (Docker exec, serial write, HTTP serve, `sysupgrade`). Preserve this ordering — it's what makes crash recovery possible per ARCHITECTURE.md.

**DUT logic is centralized in `DutWorkflow`.** Both `BuildWorkflow` (when `--allow-flash`) and `FlashWorkflow` delegate to it for serial connect, HTTP firmware serve (`transfer.py`), `wget` on the DUT, `sysupgrade`, prompt-return wait, and smoke tests. Only `transfer == "http"` is implemented; `scp`/`tftp`/`custom` raise `DutWorkflowError`.

**Config is strict, redacted, and env-interpolated.** `config.py` uses Pydantic with `extra="forbid"`. `${VAR}` and `${VAR:-default}` are expanded from the environment at load time. `OwrtConfig.redacted_dump` masks `dut.login.password` and any `builder.env` key matching the sensitive-key regex — always use the redacted dump for snapshots, reports, and logs.

**Multi-profile via deep-merge overlay.** Top-level `profiles: { name: { ...overlay... } }` block holds per-board overrides. `OwrtConfig.with_profile(name)` deep-merges the named overlay onto the base, then re-validates the result through Pydantic — so an invalid overlay surfaces a clear `ConfigError`, not silent corruption. Lists (e.g. `builder.command`, `artifact.patterns`) replace wholesale; dicts merge key-by-key. Workflows accept `profile=` and apply it once at construction; the original `OwrtConfig` is immutable. Don't add per-profile logic in workflow code — push it into the overlay so the merge stays the single source of truth.

**Path resolution is config-relative.** `OwrtConfig.artifact_root(config_path)` and `state_db_path(config_path)` resolve relative paths against the config file's parent directory, not CWD. Default state DB is `<artifact_root>/owrt_monitor.sqlite3` unless `project.state_db` is set.

**Job state is the source of truth, not iTerm/tmux.** Per ARCHITECTURE.md, never rely on terminal scraping or AppleScript for control flow. Structured events, the SQLite `jobs`/`job_events`/`artifacts`/`dut_locks`/`test_results` tables, and on-disk `report.json`/`report.md` are authoritative.

**LLM boundary.** LLM analysis is advisory only. Never let model output choose a firmware file, run `sysupgrade`, delete build dirs, or change DUT bootloader/network settings — the deterministic config and workflow stay in charge.

## Conventions

- Python target is 3.11+. Ruff is configured (`E,F,I,UP,B`, line length 100) and runs in CI — keep `make lint` clean.
- Prefer argument arrays over shell strings for subprocess/Docker calls (see `docker_build.py`). Avoid concatenating user-controlled strings into shells.
- Don't add new top-level CLI commands without wiring them through a workflow class so jobs/events/reports stay consistent.
- Tests live under `tests/python/`; pytest's `pythonpath` is already set in `pyproject.toml`, so module imports resolve without extra config when invoked via `pytest` directly. The `Makefile` also exports `PYTHONPATH=python` for safety.
- Integration test harness — `BuildWorkflow` accepts an optional `docker_client=` constructor param. Production builds a real `DockerBuildClient` from config; tests pass `tests/python/fake_docker.py:FakeDockerBuildClient` which fabricates artifacts and writes canned build.log content. End-to-end test coverage for the non-dry-run workflow path lives in `test_workflow_integration.py` — use these as the model when adding new workflow features so the integration surface stays exercised in CI.
