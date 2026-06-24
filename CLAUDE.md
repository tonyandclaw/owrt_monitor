# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

`owrt_monitor` is a Python+Go hybrid that orchestrates OpenWrt builds, firmware export, DUT flashing over USB serial, and post-upgrade smoke tests on a macOS host.

- Python (`python/owrt_monitor/`) is the workflow engine and ships the user-facing `owrt-monitor` CLI. All build/flash/test execution lives here.
- Go (`cmd/owrtd`, `cmd/owrtctl`) is a working control plane that wraps the Python engine — not a placeholder. `owrtd` is an HTTP daemon (~16 endpoints: job listing/report/events/files, locks, runner status, and `POST /v1/jobs` which launches `owrt-monitor` as a supervised subprocess). `owrtctl` is its CLI client (~14 commands). Both have substantial test suites; CI enforces a Go coverage minimum (`GO_COVER_MIN`, 85% for `cmd/owrtd`+`cmd/owrtctl`).
- Go (`cmd/owrt-engine`, `internal/*`) is additionally a **standalone engine** being grown to run the workflow natively (not just wrap Python). It reuses the *same* on-disk state as Python — pure-Go SQLite (`modernc.org/sqlite`, no cgo) on the same `.sqlite3` file, plus identical `report.json`/`events.jsonl`/`locks.json`/`config.snapshot.yaml` — so both engines interoperate over one `artifact_dir` (verified both directions). Packages: `internal/{store,config,events,report,state,artifact,docker,buildlog,workflow,analysis,configdiff}`. `cmd/owrt-engine` exposes `validate|dry-run|build|status|analyze|diff`. The destructive flash/DUT path (serial, transfer, sysupgrade+boot+smoke) is **not yet ported** — `build --allow-flash` is rejected; that phase needs on-hardware validation. New engine packages use a 70% coverage floor (`GO_ENGINE_COVER_MIN`); exec/hardware-coupled `internal/{docker,serial,dut}` are exempt from the gate. Coordinate with the roadmap in `TODO.md` and `ARCHITECTURE.md`.
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

Go (tested, runs in CI):

```sh
make test-go            # go test ./...
make test-go-cover      # go test -cover ./... and fail under GO_COVER_MIN (85%)
go build ./cmd/owrtd ./cmd/owrtctl
```

CLI entrypoint after `pip install -e` (run `owrt-monitor --help` for the full set):

```sh
owrt-monitor {validate|lab-check|dry-run|build|run|flash|test|resume|status|inspect|analyze|metrics|prune|cancel} --config configs/example.yaml
```

`run --allow-flash` and `flash --allow-flash` execute the destructive `sysupgrade` against the DUT — guard rails refuse to flash unless that flag is explicit, and `dry-run` is the safe preview.

## Architecture notes that aren't obvious from filenames

**Three workflow classes, one shared safety contract.** `workflow.py` exposes `BuildWorkflow`, `FlashWorkflow`, and `SmokeTestWorkflow`. Each one creates a fresh `job_<uuid12>` directory under `project.artifact_dir`, snapshots the (redacted) config, opens the SQLite `JobStore`, drives a `JobState` transition stream through `EventLogger` (writes to both `events.jsonl` and the `job_events` table), and calls `write_report` at the end. New workflows must follow this same shape so the `status` command, reports, and recovery story keep working.

**Go owrtd is a thin HTTP face over Python's on-disk state — it does not re-implement the engine.** Per the header comment in `cmd/owrtd/main.go`, the source of truth is the per-job run directories Python writes under `project.artifact_dir` (`report.json`, `events.jsonl`, `analysis.json`). owrtd reads those files to serve `GET` endpoints, and `POST /v1/jobs` launches `owrt-monitor` as a supervised child process (the runner) — Python stays the workflow engine until a later phase migrates execution into Go. SQLite is intentionally *not* opened from Go to avoid a cgo/pure-Go dependency. owrtd binds loopback (127.0.0.1) by default and assumes no TLS; front it with a reverse proxy if exposed. `owrtctl` is just an HTTP client for these endpoints. When adding Go endpoints, keep reads file-based and don't duplicate workflow logic that belongs in Python.

**State transitions persist before side effects.** `_transition` writes to SQLite and emits a JSONL event before any external action (Docker exec, serial write, HTTP serve, `sysupgrade`). Preserve this ordering — it's what makes crash recovery possible per ARCHITECTURE.md.

**DUT logic is centralized in `DutWorkflow`.** Both `BuildWorkflow` (when `--allow-flash`) and `FlashWorkflow` delegate to it for serial connect, firmware transfer (`transfer.py`), the download command on the DUT, `sysupgrade`, prompt-return wait, and smoke tests. Five `upgrade.transfer` modes are implemented (`http`, `tftp`, `bootloader_tftp`, `scp`, `custom`), each with a real-execution branch and a dry-run preview branch in `dut_workflow.py`; an unknown mode raises `DutWorkflowError`. `http`/`tftp`/`bootloader_tftp` serve from the host over the DUT-facing interface, so they go through `infer_host_for_interface` to pick the host IP.

**Config is strict, redacted, and env-interpolated.** `config.py` uses Pydantic with `extra="forbid"`. `${VAR}` and `${VAR:-default}` are expanded from the environment at load time. `OwrtConfig.redacted_dump` masks `dut.login.password` and any `builder.env` key matching the sensitive-key regex — always use the redacted dump for snapshots, reports, and logs.

**Multi-profile via deep-merge overlay.** Top-level `profiles: { name: { ...overlay... } }` block holds per-board overrides. `OwrtConfig.with_profile(name)` deep-merges the named overlay onto the base, then re-validates the result through Pydantic — so an invalid overlay surfaces a clear `ConfigError`, not silent corruption. Lists (e.g. `builder.command`, `artifact.patterns`) replace wholesale; dicts merge key-by-key. Workflows accept `profile=` and apply it once at construction; the original `OwrtConfig` is immutable. Don't add per-profile logic in workflow code — push it into the overlay so the merge stays the single source of truth.

**Path resolution is config-relative.** `OwrtConfig.artifact_root(config_path)` and `state_db_path(config_path)` resolve relative paths against the config file's parent directory, not CWD. Default state DB is `<artifact_root>/owrt_monitor.sqlite3` unless `project.state_db` is set.

**Job state is the source of truth, not iTerm/tmux.** Per ARCHITECTURE.md, never rely on terminal scraping or AppleScript for control flow. Structured events, the SQLite `jobs`/`job_events`/`artifacts`/`dut_locks`/`test_results` tables, and on-disk `report.json`/`report.md` are authoritative.

**LLM boundary.** LLM analysis is advisory only. Never let model output choose a firmware file, run `sysupgrade`, delete build dirs, or change DUT bootloader/network settings — the deterministic config and workflow stay in charge.

## Conventions

- Python target is 3.11+. Ruff is configured (`E,F,I,UP,B`, line length 100) and runs in CI — keep `make lint` clean.
- Prefer argument arrays over shell strings for subprocess/Docker calls (see `docker_build.py`). Avoid concatenating user-controlled strings into shells.
- Commands that *execute* (build/run/flash/test) must go through a workflow class so jobs/events/reports stay consistent. Read-only/reporting commands (`status`, `inspect`, `analyze`, `metrics`, `prune`, `cancel`) instead read the persisted job state (SQLite + on-disk reports) and live in their own modules (`inspect.py`, `analysis.py`, `metrics.py`, `retention.py`, `cancel.py`) — don't make them re-run work.
- Tests live under `tests/python/`; pytest's `pythonpath` is already set in `pyproject.toml`, so module imports resolve without extra config when invoked via `pytest` directly. The `Makefile` also exports `PYTHONPATH=python` for safety.
- Integration test harness — `BuildWorkflow` accepts an optional `docker_client=` constructor param. Production builds a real `DockerBuildClient` from config; tests pass `tests/python/fake_docker.py:FakeDockerBuildClient` which fabricates artifacts and writes canned build.log content. End-to-end test coverage for the non-dry-run workflow path lives in `test_workflow_integration.py` — use these as the model when adding new workflow features so the integration surface stays exercised in CI.
