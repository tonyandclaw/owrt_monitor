# Quickstart

This MVP starts with a safe host-side path, then optionally continues into DUT flashing when
`--allow-flash` is explicitly provided:

1. Validate a YAML config.
2. Create a job record in SQLite.
3. Dry-run or execute a Docker build.
4. Detect firmware artifacts in the OpenWrt build tree.
5. Export the selected artifact and write reports.
6. Optionally serve firmware over a temporary host HTTP server.
7. Optionally drive the DUT over serial, run the configured upgrade command, and run tests.

## Install

```sh
python3 -m pip install -e ".[dev,serial]"
```

## Validate Config

```sh
owrt-monitor validate --config configs/example.yaml
owrt-monitor validate --config configs/example.yaml --profile gateway
```

`validate` with no `--profile` applies `project.default_profile` when configured and lists
the available profiles defined in the config's `profiles:` block (e.g. `ap-be5000`,
`ap-be14000`, `controller`, `gateway`, `switch`, `ap-mt76`).

## Profiles

A single config can host multiple board profiles. Each profile is deep-merged onto the base
config when selected via `--profile <name>`. Lists (`builder.command`, `artifact.patterns`)
are replaced wholesale; nested dicts merge key-by-key.

```sh
owrt-monitor build --config configs/example.yaml
owrt-monitor build --config configs/example.yaml --profile switch
owrt-monitor flash --config configs/example.yaml --profile gateway \
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
also previews the serial, transfer, upgrade, and test actions.

## Build and Export Firmware

Before touching hardware, run the local lab readiness check:

```sh
owrt-monitor lab-check --config configs/example.yaml
OWRT_DUT_SERIAL=/dev/cu.usbserial-8330 owrt-monitor lab-check --config configs/example.yaml
```

It verifies the builder container, serial device selection, DUT network
reachability, configured firmware transfer path, and that the serial console
responds with the configured shell prompt before any build or flash work starts.

```sh
owrt-monitor build --config configs/example.yaml
```

The configured builder container must be running and must provide `python3` for the MVP artifact
detector. The detector runs inside the builder workdir and evaluates the configured glob patterns.

To let the Go daemon supervise the workflow process, start `owrtd` and submit
through the local API:

```sh
go run ./cmd/owrtd --artifacts-dir artifacts
owrt-monitor build --config configs/example.yaml --daemon-url http://127.0.0.1:8765
```

The default path still runs directly in the Python CLI; `--daemon-url` is the
opt-in Go runner path.

The daemon also serves a read-only dashboard for job history, runner state,
analysis, and log tails:

```sh
open http://127.0.0.1:8765/ui/
```

Use `owrtctl` when the dashboard is too heavy and `curl` is too awkward:

```sh
go run ./cmd/owrtctl -- health
go run ./cmd/owrtctl -- build
go run ./cmd/owrtctl -- run
go run ./cmd/owrtctl -- flash --profile ap-be5000
go run ./cmd/owrtctl -- test
go run ./cmd/owrtctl -- dry-run
go run ./cmd/owrtctl -- jobs --limit 10
go run ./cmd/owrtctl -- status <job_id>
go run ./cmd/owrtctl -- wait <job_id>
go run ./cmd/owrtctl -- logs <job_id> --tail 80 --follow
go run ./cmd/owrtctl -- report <job_id>
go run ./cmd/owrtctl -- analysis <job_id>
go run ./cmd/owrtctl -- events <job_id>
go run ./cmd/owrtctl -- file <job_id> report.md --output report.md
go run ./cmd/owrtctl -- cancel <job_id>
go run ./cmd/owrtctl -- remove <job_id>
go run ./cmd/owrtctl -- locks
```

`owrtctl` talks to `http://127.0.0.1:8765` by default. Override that with
`--daemon-url URL` or `OWRTD_URL`. `wait` exits non-zero if the supervised
runner exits non-zero, fails to start, becomes orphaned, or times out.

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
- Runs configured smoke, script, pytest, and SSH tests.

Use this only after validating `dut.serial`, `dut.prompt`, the transfer host field
(`upgrade.http_host`, `upgrade.tftp_host`, or `upgrade.scp_host`), and `upgrade.command`
for the specific board.

## Flash Existing Firmware

```sh
owrt-monitor flash --config configs/example.yaml --dry-run
owrt-monitor flash --config configs/example.yaml
```

`flash` skips Docker and uses an existing host firmware file. If `--artifact`
is omitted, it uses the newest successful exported artifact, filtered by
`--profile` when provided.

## Tests Only

```sh
owrt-monitor test --config configs/example.yaml
```

This opens the DUT serial console, waits for the prompt, runs `tests.smoke`, then runs any
configured host-side `tests.scripts`, `tests.pytest`, and `tests.ssh` entries.

## Status

```sh
owrt-monitor status --config configs/example.yaml
```

Job state is stored in `artifacts/owrt_monitor.sqlite3` unless `project.state_db` is configured.
The `Alive` column shows `yes`/`no` for non-terminal jobs based on whether the recorded workflow
PID is still running (orphan jobs from a crash will show `no`).

To mark dead non-terminal jobs as failed orphans and release any locks they still own:

```sh
owrt-monitor status --config configs/example.yaml --mark-orphans
```

## Advisory Analysis

Generate a redacted, structured analysis bundle for a completed or failed job:

```sh
owrt-monitor analyze <job_id> --config configs/example.yaml
owrt-monitor analyze artifacts/job_x
```

This writes `analysis.json` and `analysis.md` into the run directory. The output
is advisory only: it preserves source file hashes, evidence line references,
suggested next actions, and a redacted bug report draft for a future UI/LLM
layer, but it never authorizes destructive actions.

When `owrtd` is running, the generated JSON is available through the read-only
daemon API:

```sh
curl http://127.0.0.1:8765/v1/jobs/<job_id>/analysis
```

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
