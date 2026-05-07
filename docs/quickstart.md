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
```

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
