# Config Reference

`owrt_monitor` uses YAML. Values can interpolate environment variables with `${NAME}` or
`${NAME:-default}`. Pydantic strict-validates with `extra="forbid"` — typos in field names
fail loudly at load time.

## project

- `name`: Human-readable lab or project name.
- `artifact_dir`: Host directory where job logs, firmware exports, and reports are written.
- `state_db`: Optional SQLite path. Defaults to `<artifact_dir>/owrt_monitor.sqlite3`.

## builder

- `container`: Docker container name or ID.
- `workdir`: OpenWrt / firmware tree path inside the container.
- `command`: Build command as an argument array. First element is the executable.
- `env`: Optional environment variables passed to `docker exec`. Keys matching
  `(password|secret|token|api_key|private_key)` are redacted in snapshots and reports.
- `timeout_sec`: Build timeout. `0` disables the timeout.
- `min_free_disk_mb`: Minimum free MB on `workdir`'s filesystem before preflight passes.
  Default `5000`. Set to `0` to disable. Uses `df -B1 --output=avail` (GNU coreutils);
  silently skipped if `df` introspection fails inside the container.

## artifact

- `patterns`: Glob patterns evaluated inside `builder.workdir`. Bash `globstar` (`**`) is
  supported; the detector uses bash + GNU `stat`, no python required in the container.
- `selection`: `newest`, `largest`, or `fail-if-multiple`.
- `min_size_mb`: Minimum firmware size in MB.
- `require_sha256`: Reserved safety flag. The MVP always computes host SHA256 after export.
- `export_filename`: Optional output filename override.

## dut

- `name`: DUT lock name (used for the SQLite lock; one job per name at a time).
- `serial`: USB serial path. If omitted, `discovery_patterns` must match exactly one port.
- `baud`: Serial baud rate.
- `prompt`: Regex used to detect a ready shell prompt.
- `newline`: `lf` or `crlf`.
- `connect_timeout_sec`: Timeout for initial prompt detection.
- `command_timeout_sec`: Timeout for non-transfer serial commands.
- `lock_timeout_sec`: How old (in seconds) a DUT lock's heartbeat may be before
  `acquire_dut_lock` will break it on the next attempt. Default `1800` (30 min). Recovers
  from crashed prior owners without manual cleanup.
- `discovery_patterns`: Host glob patterns used for serial auto-discovery.
- `login.username` / `login.password`: Username/password metadata. Automated password login
  is not yet implemented; `password` is captured in snapshots as `<redacted>`.
- `network.address`: DUT IP used for host IP inference when `upgrade.http_host` /
  `upgrade.tftp_host` is omitted.
- `network.interface`: Informational interface name for reports/config clarity.

## upgrade

- `transfer`: `http` (default) or `tftp`. `scp` and `custom` are reserved.
- `remote_path`: Firmware path on the DUT.
- `command`: Upgrade command written to the DUT serial shell. **Destructive.**
- `boot_timeout_sec`: Timeout while waiting for the prompt after the upgrade command.
- `transfer_timeout_sec`: Timeout for the DUT firmware download command (`wget` or `tftp`).
- `verify_sha256`: Run `sha256sum` on the DUT after transfer to confirm bytes match.
- `min_dut_free_kb`: Minimum free KB at the firmware's remote directory before transfer.
  Default `0` (disabled). Threshold actually applied is `max(min_dut_free_kb, firmware_size_kb)`,
  so even a small explicit setting still guards against an undersized `/tmp`.
- `boot_failure_patterns`: List of regex strings checked against the boot stream during
  reboot wait. Any match raises `BootFailureError` immediately with the offending line as
  evidence, instead of waiting out `boot_timeout_sec`. Default catches Linux kernel panics,
  Oops, paging-request faults, etc. Regexes use `re.MULTILINE`.

### upgrade — HTTP transfer

- `http_bind`: Host address for the temporary firmware HTTP server. Default `0.0.0.0`.
- `http_host`: Host IP or DNS name reachable by the DUT. If omitted, owrt_monitor tries to
  infer it from `dut.network.address`.
- `http_port`: Host HTTP port. `0` asks the OS for a free port.

### upgrade — TFTP transfer

- `tftp_root`: Host directory where the workflow `cp`s the firmware so the system tftpd can
  serve it. Default `/private/tftpboot` (macOS launchd-managed `tftpd`'s default root).
  Must already exist and be writable; the workflow will not create it.
- `tftp_host`: Host IP reachable by the DUT for TFTP. Falls back to `http_host`, then to
  inference from `dut.network.address`. The DUT command shape is
  `tftp -g -r <filename> -l <remote_path> <tftp_host>` (BusyBox-friendly).

## tests

- `smoke`: Serial shell commands to run after upgrade or through `owrt-monitor test`.
- `command_timeout_sec`: Timeout for each smoke command.

## retry

Per-step retry policy. Each step takes `attempts` (default `1` = no retry) and
`backoff_sec` (default `0`). Backoff is cancellation-aware. `JobCancelled` is never retried.

- `artifact_select`: Retries `list_artifacts` + `select_artifact`.
- `artifact_export`: Retries `docker cp`.
- `firmware_transfer`: Retries the DUT-side download command + verification.
- `smoke_tests`: Retries each individual smoke-test command.

The OpenWrt `make` build itself is intentionally **not** wrapped in retry — it's expensive
to repeat blindly. The destructive `sysupgrade` is also intentionally not retried.

## profiles

Top-level `profiles: { name: { ...overlay... } }` block. Each overlay deep-merges onto the
base config when the user passes `--profile <name>`. List values (e.g. `builder.command`,
`artifact.patterns`) are replaced wholesale; nested dicts merge key-by-key.

```yaml
profiles:
  ap:
    builder:
      command: [make, owrt2102.asus_mt_wifi7_mt7987]
    artifact:
      patterns:
        - build/owrt2102/bin/target/openwrt-*-mediatek_mt7987a-emmc-squashfs-sysupgrade.bin
    upgrade:
      transfer: tftp
      tftp_host: 192.168.1.66
```

`--profile` is accepted by `validate`, `dry-run`, `build`, `run`, `flash`, `test`, `resume`.
The applied profile name is captured in the per-job provenance (`build_metadata.profile`).
