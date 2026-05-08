# Quickstart

This MVP starts with a safe host-side path, then optionally continues into DUT flashing when
`--allow-flash` is explicitly provided:

1. Validate a YAML config.
2. Create a job record in SQLite.
3. Dry-run or execute a Docker build.
4. Detect firmware artifacts in the OpenWrt build tree.
5. Export the selected artifact and write reports.
6. Optionally serve firmware over a temporary host HTTP server.
7. Optionally drive the DUT over serial, run the configured upgrade command, and run smoke tests.

## Install

```sh
python3 -m pip install -e ".[dev,serial]"
```

## Validate Config

```sh
owrt-monitor validate --config configs/example.yaml
owrt-monitor validate --config configs/example.yaml --profile ap
```

`validate` with no `--profile` lists the available profiles defined in the config's
`profiles:` block (e.g. `ap`, `controller`, `switch`).

## Profiles

A single config can host multiple board profiles. Each profile is deep-merged onto the base
config when selected via `--profile <name>`. Lists (`builder.command`, `artifact.patterns`)
are replaced wholesale; nested dicts merge key-by-key.

```sh
owrt-monitor build --config configs/example.yaml --profile ap
owrt-monitor build --config configs/example.yaml --profile switch
owrt-monitor flash --config configs/example.yaml --profile controller \
    --artifact artifacts/job_x/firmware/openwrt.bin --allow-flash
```

`--profile` is accepted by `validate`, `dry-run`, `build`, `run`, `flash`, `test`, and `resume`.

## Dry Run

```sh
owrt-monitor dry-run --config configs/example.yaml
owrt-monitor run --config configs/example.yaml --dry-run --allow-flash
```

Dry-run writes a job directory under `artifacts/` with:

- `config.snapshot.yaml`
- `events.jsonl`
- `report.json`
- `report.md`

Plain `dry-run` does not call Docker and does not touch a DUT. `run --dry-run --allow-flash`
also previews the serial, transfer, upgrade, and smoke-test actions.

## Build and Export Firmware

```sh
owrt-monitor build --config configs/example.yaml
```

The configured builder container must be running and must provide `python3` for the MVP artifact
detector. The detector runs inside the builder workdir and evaluates the configured glob patterns.

## Build, Flash, and Test

```sh
owrt-monitor run --config configs/example.yaml --allow-flash
```

This command builds and exports firmware first. It then:

- Acquires the configured DUT lock.
- Opens the configured USB serial console.
- Starts a temporary host HTTP server for the exported firmware directory.
- Runs `wget` on the DUT to download the image.
- Verifies size and SHA256 when enabled.
- Runs the configured upgrade command.
- Waits for the prompt to return.
- Runs configured smoke tests.

Use this only after validating `dut.serial`, `dut.prompt`, `upgrade.http_host`, and
`upgrade.command` for the specific board.

## Flash Existing Firmware

```sh
owrt-monitor flash --config configs/example.yaml --artifact artifacts/job_x/firmware/openwrt.bin --dry-run
owrt-monitor flash --config configs/example.yaml --artifact artifacts/job_x/firmware/openwrt.bin --allow-flash
```

`flash` skips Docker and uses an existing host firmware file.

## Smoke Tests Only

```sh
owrt-monitor test --config configs/example.yaml
```

This opens the DUT serial console, waits for the prompt, and runs `tests.smoke`.

## Status

```sh
owrt-monitor status --config configs/example.yaml
```

Job state is stored in `artifacts/owrt_monitor.sqlite3` unless `project.state_db` is configured.
The `Alive` column shows `yes`/`no` for non-terminal jobs based on whether the recorded workflow
PID is still running (orphan jobs from a crash will show `no`).

## Cancelling and Resuming

Each job's run directory hosts a cancellation marker (`cancel.flag`) that the running workflow
polls between steps. Request cancellation:

```sh
owrt-monitor cancel <job_id>
```

The job will land in state `CANCELLED`. If a workflow is wedged on a non-cancellable read, the
recorded PID is printed so you can `kill -TERM` it manually.

To resume a job that completed the build but failed during flash, or stopped between
`BUILD_SUCCEEDED` and `ARTIFACT_EXPORTED`:

```sh
owrt-monitor resume <job_id> --dry-run
owrt-monitor resume <job_id> --allow-flash
```

Resume reuses the original run directory and replays from the stored config snapshot, so a
config edit between attempts will not change semantics. Resume is currently supported only when
the last persisted state is `BUILD_SUCCEEDED`, `ARTIFACT_SELECTED`, or `ARTIFACT_EXPORTED`;
DUT-phase resume requires manual recovery because device state is unknown after a crash.

## Stale DUT Locks

DUT locks are kept in SQLite. If a job crashes while holding a lock, the next run will find the
lock stale once the heartbeat is older than `dut.lock_timeout_sec` (default 1800 s) and break it
automatically. Set `dut.lock_timeout_sec: 0` is rejected; tighten or loosen as appropriate for
your boot/upgrade durations.
