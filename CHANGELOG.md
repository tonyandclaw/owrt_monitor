# Changelog

All notable changes to `owrt_monitor` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — Unreleased

The first usable cut. End-to-end Python orchestration of an OpenWrt build →
firmware export → DUT flash → smoke test cycle, with the full safety ladder
around the destructive step.

### CLI

Eleven commands. All accept `--profile <name>` to apply a profile overlay.

- **`validate`** — strict YAML schema check, lists available profiles.
- **`dry-run`** — write a planned job report without touching Docker or a DUT.
- **`build`** — run the Docker build, classify the build log, export the
  selected firmware artifact with SHA256 + provenance metadata.
- **`run`** — build/export and optionally `--allow-flash` the configured DUT
  end-to-end (DUT lock → serial → transfer → upgrade → boot wait → smoke).
- **`flash`** — flash an existing firmware to the DUT (skips the build).
- **`test`** — run smoke tests over the DUT serial console only.
- **`resume <job_id>`** — resume a previous job from `BUILD_SUCCEEDED`,
  `ARTIFACT_SELECTED`, or `ARTIFACT_EXPORTED`. DUT-phase resume intentionally
  not supported.
- **`cancel <job_id>`** — cooperative cancellation via marker file under the
  job's run directory.
- **`status`** — recent jobs as a Rich table with PID alive/dead detection.
- **`metrics`** — aggregate success rate + mean/median/p90 durations across
  recent jobs.
- **`inspect <job_id>`** — single-job summary; `--diff <other>` for side-by-side.
- **`prune`** — remove old run directories by per-result keep counts; dry-run
  by default, `--apply` to actually delete.

### Configuration

- Strict Pydantic validation with `extra="forbid"` — typos fail at load.
- `${VAR}` and `${VAR:-default}` env interpolation.
- Top-level `profiles:` block with deep-merge overlay; `--profile <name>` selects.
- Sensitive keys (`password`, `secret`, `token`, `api_key`, `private_key`) are
  redacted in the snapshot stored alongside every job.

### Build pipeline

- `DockerBuildClient` runs `docker exec`, streams stdout/stderr to `build.log`.
- Bash-only artifact detector (`shopt -s globstar nullglob` + `stat`). No
  python3 needed inside the builder container.
- Disk-space preflight (`builder.min_free_disk_mb`) — `df -B1 --output=avail`.
- Concurrent-build prevention (`builder_locks` table + `builder.lock_timeout_sec`).
- Build-log classifier (`success`, `disk_full`, `failed_package`, `compile_error`,
  `unknown`) populates `report.md`'s `## Build Log` section. Real-world
  signature for disk_full grounded in actual lab failure logs.
- Build duration parsed from `>>>> ... Build done in: MM:SS.fff`.
- Per-job provenance: git commit / dirty / describe captured via best-effort
  `git rev-parse` inside the builder, plus `built_at` / `make_target` / `profile`.

### Firmware transfer

Three modes:

- **`http`** — temporary host HTTP server, DUT runs `wget -O ...`.
- **`tftp`** — host-side `cp` to `tftp_root` (default `/private/tftpboot`),
  DUT runs `tftp -g -r ... -l ... <host>` (BusyBox-friendly).
- **`bootloader_tftp`** — drop into U-Boot via shell `reboot` + autoboot
  interrupt, run `setenv serverip ...; setenv ipaddr ...; tftpboot <addr>
  <name>; bootm`. Volatile boot (no flash write); useful for testing
  candidate firmware.

Per-job firmware copy is preserved alongside any TFTP publish (audit trail
+ SHA256 verifiable on host).

### Safety ladder around `sysupgrade`

Independent layers, each catches a different failure mode:

1. **Pre-flash gate** — `dut.expected_artifact_pattern` regex checked against
   the selected artifact's filename. Catches "wrong variant flashed".
2. **DUT `/tmp` space** — `upgrade.min_dut_free_kb` checked via BusyBox
   `df -k` before `wget`/`tftp`.
3. **SHA256 verify** — `sha256sum <remote> | grep -i ^<expected>` on the DUT
   after transfer; mismatch aborts before sysupgrade.
4. **Boot-failure fingerprints** — `upgrade.boot_failure_patterns` scanned
   continuously during the reboot wait. `Kernel panic`, `Oops`, paging
   faults trigger immediate `BootFailureError` instead of timing out.
5. **Post-boot positive markers** — `upgrade.expected_boot_markers` regex
   list must all appear in the boot transcript. Catches "device booted
   recovery slot" and similar wrong-image scenarios.
6. **Post-boot status capture** — `ubus call system board` parsed for
   kernel/release/hostname/board, written to `report.md`'s `## DUT Status`.
7. **Smoke-test assertions** — `tests.smoke[].expect` regex per command;
   pass requires output match, not just successful exit.

### Reliability

- Cooperative cancellation via per-job `cancel.flag` marker file. Cancelled
  jobs land in `JobState.CANCELLED`, not `FAILED`. Long subprocesses
  (Docker build) get a watchdog thread that calls `process.terminate()`.
- DUT lock with stale-recovery (`dut.lock_timeout_sec`, default 1800 s).
- Builder lock with stale-recovery (`builder.lock_timeout_sec`, default 3600 s).
- Per-step retry policy (`retry.{artifact_select, artifact_export,
  firmware_transfer, smoke_tests}` — `attempts` + `backoff_sec`). Retries
  are cancellation-aware; `JobCancelled` propagates immediately.
- Login flow with optional username/password. Password is redacted in
  the on-disk `serial.log` even though the device sees the real bytes.
- Resume from `BUILD_SUCCEEDED` / `ARTIFACT_SELECTED` / `ARTIFACT_EXPORTED`.
  Reuses the original run directory and stored config snapshot — config
  edits between attempts don't change semantics mid-job.

### Observability

- `report.md` and `report.json` per job, with sections for `Artifact`,
  `Provenance`, `Metrics`, `Build Log`, `DUT Status`, `Smoke Tests`,
  `Actions`, `Warnings`.
- `events.jsonl` with structured state transitions and component events.
- Metrics persisted in SQLite (`jobs.metrics` JSON column). `owrt-monitor
  metrics` aggregates success rate + per-metric mean/median/p90/min/max
  across recent jobs.
- `inspect <job> --diff <other>` for side-by-side comparison; correctly
  distinguishes "absent" from "different" via `?` vs `no` markers.
- Config diff vs last successful run surfaced in the actions list at job
  start.

### Persistence

- SQLite tables: `jobs`, `job_events`, `artifacts`, `dut_locks`,
  `builder_locks`, `test_results`. Idempotent migrations via
  `_init_db` + `_migrate`.
- Per-job `run_dir`: `config.snapshot.yaml`, `events.jsonl`, `build.log`,
  `serial.log`, `firmware/<name>.bin`, `report.json`, `report.md`,
  `cancel.flag` (when cancelled).

### Testing

- 159 Python tests covering: config validation, profile merge, retry/cancel
  semantics, build-log classifier (with real lab fixtures), DUT
  free-space parser, lock behavior, login dance, boot-marker detection,
  bootloader-TFTP dance, status capture, metrics aggregation, inspect/diff,
  prune planning, all three transfer modes end-to-end via fakes.
- Integration test harness (`tests/python/fake_docker.py:FakeDockerBuildClient`)
  lets `BuildWorkflow.run(allow_flash=True)` exercise every state transition
  in CI without docker, hardware, or network.
- 12-state state-machine assertion via `events.jsonl` + JSON-extract SQL.

### Documentation

- `README.md` — entry point with command quick-reference.
- `ARCHITECTURE.md` — system diagram, language split, state machine,
  workflow flows.
- `docs/quickstart.md` — install, validate, dry-run, build, run, flash,
  test, status, profiles, cancel/resume, stale-lock recovery.
- `docs/config-reference.md` — every YAML field documented.
- `docs/lab-setup.md` — host requirements, builder container facts,
  TFTP setup, disk hygiene.
- `docs/safe-upgrade.md` — pre-flash checklist; what runs in what order.
- `docs/troubleshooting.md` — failure recipes keyed off classifier output
  (disk_full, failed_package, panic, orphan jobs, tftp setup).
- `docs/adding-a-new-board.md` — profile template + verification sequence.
- `CLAUDE.md` — repo conventions for future Claude Code instances.

### Known limitations

- DUT-phase resume not supported (device state is undetermined after a
  partial flash).
- Bootloader-TFTP recovery is volatile boot only (no flash write yet).
- Custom transfer command not implemented (placeholder in the schema).
- Go runner / daemon (`owrtd`, `owrtctl`) is a stub. The Python-only
  workflow is the daily-driver path.
- iTerm2/tmux observer integration is not implemented.
- LLM analysis hooks are not implemented.
- End-to-end automated test against a real DUT requires hardware in the
  lab; current 159 tests use fakes.
