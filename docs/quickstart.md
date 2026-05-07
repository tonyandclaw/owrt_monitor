# Quickstart

This MVP starts with the safe host-side path:

1. Validate a YAML config.
2. Create a job record in SQLite.
3. Dry-run or execute a Docker build.
4. Detect firmware artifacts in the OpenWrt build tree.
5. Export the selected artifact and write reports.

DUT flashing and post-upgrade serial tests are intentionally reserved for the next milestone.

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
```

Dry-run writes a job directory under `artifacts/` with:

- `config.snapshot.yaml`
- `events.jsonl`
- `report.json`
- `report.md`

It does not call Docker and does not touch a DUT.

## Build and Export Firmware

```sh
owrt-monitor build --config configs/example.yaml
```

The configured builder container must be running and must provide `python3` for the MVP artifact
detector. The detector runs inside the builder workdir and evaluates the configured glob patterns.

## Status

```sh
owrt-monitor status --config configs/example.yaml
```

Job state is stored in `artifacts/owrt_monitor.sqlite3` unless `project.state_db` is configured.

