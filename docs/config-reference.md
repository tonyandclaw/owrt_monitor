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

- `name`: DUT lock name.
- `serial`: USB serial path. If omitted, `discovery_patterns` must match exactly one port.
- `baud`: Serial baud rate.
- `prompt`: Regex used to detect a ready shell prompt.
- `newline`: `lf` or `crlf`.
- `connect_timeout_sec`: Timeout for initial prompt detection.
- `command_timeout_sec`: Timeout for non-transfer serial commands.
- `discovery_patterns`: Host glob patterns used for serial auto-discovery.
- `login`: Username/password metadata. Automated password login is not implemented yet.
- `network.address`: DUT network address used for host IP inference when `upgrade.http_host` is omitted.
- `network.interface`: Informational interface name for reports/config clarity.

## upgrade

- `transfer`: Currently `http` is implemented.
- `remote_path`: Firmware path on the DUT.
- `command`: Upgrade command written to the DUT serial shell. This is destructive.
- `boot_timeout_sec`: Timeout while waiting for the prompt after the upgrade command.
- `transfer_timeout_sec`: Timeout for the DUT firmware download command.
- `http_bind`: Host address for the temporary firmware HTTP server.
- `http_host`: Host IP or DNS name reachable by the DUT. If omitted, owrt_monitor tries to infer it
  from `dut.network.address`.
- `http_port`: Host HTTP port. `0` asks the OS for a free port.
- `verify_sha256`: Run `sha256sum` on the DUT after transfer.

## tests

- `smoke`: Serial shell commands to run after upgrade or through `owrt-monitor test`.
- `command_timeout_sec`: Timeout for each smoke command.
