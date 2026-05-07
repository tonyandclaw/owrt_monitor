# Config Reference

`owrt_monitor` uses YAML. Values can interpolate environment variables with `${NAME}` or
`${NAME:-default}`.

## project

- `name`: Human-readable lab or project name.
- `artifact_dir`: Host directory where job logs, firmware exports, and reports are written.
- `state_db`: Optional SQLite path. Defaults to `<artifact_dir>/owrt_monitor.sqlite3`.

## builder

- `container`: Docker container name or ID.
- `workdir`: OpenWrt tree path inside the container.
- `command`: Build command as an argument array.
- `env`: Optional environment variables passed to `docker exec`.
- `timeout_sec`: Build timeout. `0` disables the timeout.

## artifact

- `patterns`: Recursive glob patterns evaluated inside `builder.workdir`.
- `selection`: `newest`, `largest`, or `fail-if-multiple`.
- `min_size_mb`: Minimum firmware size.
- `require_sha256`: Reserved safety flag. The MVP always computes host SHA256 after export.
- `export_filename`: Optional output filename override.

## dut

Serial and network settings for the future firmware upgrade milestone.

## upgrade

Firmware transfer and upgrade command settings for the future firmware upgrade milestone.

## tests

Post-upgrade smoke commands for the future test milestone.

