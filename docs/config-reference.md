# Config Reference

`owrt_monitor` uses YAML. Values can interpolate environment variables with `${NAME}` or
`${NAME:-default}`. Pydantic strict-validates with `extra="forbid"` — typos in field names
fail loudly at load time.

## project

- `name`: Human-readable lab or project name.
- `artifact_dir`: Host directory where job logs, firmware exports, and reports are written.
- `state_db`: Optional SQLite path. Defaults to `<artifact_dir>/owrt_monitor.sqlite3`.
- `default_profile`: Optional profile name to apply when a command omits `--profile`.
  The example config defaults to `ap-be5000`.

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

- `transfer`: `http` (default), `scp`, `tftp`, `bootloader_tftp`, or `custom`.
- `remote_path`: Firmware path on the DUT.
- `command`: Upgrade command written to the DUT serial shell. **Destructive.**
- `boot_timeout_sec`: Timeout while waiting for the prompt after the upgrade command.
- `transfer_timeout_sec`: Timeout for the firmware transfer command.
- `verify_sha256`: Run `sha256sum` on the DUT after transfer to confirm bytes match.
- `min_dut_free_kb`: Minimum free KB at the firmware's remote directory before transfer.
  Default `0` (disabled). Threshold actually applied is `max(min_dut_free_kb, firmware_size_kb)`,
  so even a small explicit setting still guards against an undersized `/tmp`.
- `host_interface`: Host network interface, macOS network service, or macOS hardware port
  whose current IPv4 should be used as the firmware host for HTTP/TFTP transfer. Examples:
  `bridge100`, `en7`, or `USB 10/100/1000 LAN`. When set, this dynamic address takes
  precedence over `http_host` / `tftp_host`, and `owrt-monitor build` resolves it before
  the Docker build starts so stale lab IPs fail early.
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

- `tftp_root`: Host directory where the workflow `cp`s the firmware before serving it.
  Default `/private/tftpboot`. Must already exist and be writable; the workflow will not
  create it. For OpenWrt-shell `transfer: tftp`, `owrt-monitor` serves this directory with
  a temporary read-only TFTP server.
- `tftp_host`: Host IP reachable by the DUT for TFTP and `bootloader_tftp`. Falls back
  to `http_host`, then to inference from `dut.network.address`.
- `tftp_port`: Host TFTP port for OpenWrt-shell `transfer: tftp`. `0` asks the OS for a
  free high port and the DUT command uses BusyBox's `HOST PORT` form. `bootloader_tftp`
  ignores this and still expects the host's normal TFTP service on UDP/69.
  The OpenWrt-shell TFTP command shape is
  `tftp -g -r <filename> -l <remote_path> <tftp_host> <tftp_port>`.
- `network_recovery`: Optional runtime-only rescue before HTTP/TFTP transfer. When
  `enabled: true`, the workflow pings `ping_host` (or the transfer host) from the DUT
  serial console. If unreachable, it treats `interface` and `static_cidr` (for example
  `192.168.1.1/24`) as recovery hints, temporarily adds that address to `interface`,
  verifies reachability, transfers the firmware, then removes the temporary IP when
  `restore_after_transfer` is true. The UCI proto is logged as context; console
  reachability is the source of truth.
- `post_upgrade_network`: Optional persistent post-boot normalization after a successful
  firmware upgrade. When `ensure_dhcp: true`, the workflow waits for the upgraded image to
  return to the serial prompt, finds the UCI network section for `interface` (or
  `dut.network.interface`), sets `proto=dhcp`, deletes common static address fields,
  commits `network`, reloads/restarts networking, and verifies the section is DHCP before
  status capture and smoke tests.

### upgrade — SCP transfer

- `scp_binary`: SCP executable. Default `scp`.
- `scp_user`: Remote username. Default `root`.
- `scp_host`: DUT host/IP for SCP. Falls back to `dut.network.address`.
- `scp_port`: SSH/SCP port. Default `22`.
- `scp_identity_file`: Optional identity file passed as `-i <path>`.
- `scp_extra_args`: Extra arguments inserted before the source/target. For OpenWrt
  Dropbear targets that do not support SFTP, set `scp_extra_args: ["-O"]` to force
  legacy SCP mode on modern OpenSSH clients.

### upgrade — custom transfer

- `custom_transfer_command`: Host-side command as an argument array. Required when
  `transfer: custom`. The command is executed directly, without an implicit shell, and
  must place the firmware at `remote_path` on the DUT. The workflow still verifies size
  and SHA256 over serial after the command returns.
- Supported placeholders in each argument: `{artifact}` / `{artifact_path}` (host firmware
  path), `{filename}`, `{sha256}`, `{size_bytes}`, `{remote_path}`, `{dut_name}`,
  `{dut_serial}`, `{dut_address}`, `{run_dir}`, `{job_id}`.
  Unknown placeholders fail config validation; use `{{` / `}}` when a literal brace
  is needed inside an argument.

Example:

```yaml
upgrade:
  transfer: custom
  remote_path: /tmp/firmware.bin
  custom_transfer_command:
    - sh
    - -c
    - 'my-transfer-tool "$1" "$2"'
    - sh
    - '{artifact}'
    - '{dut_address}:{remote_path}'
```

## tests

- `smoke`: Serial shell commands to run after upgrade or through `owrt-monitor test`.
- `scripts`: Host-side executables run after smoke tests. Each receives DUT/job context
  through `OWRT_*` environment variables.
- `pytest`: Host-side pytest invocations run as `<current Python> -m pytest <path> ...`
  after smoke tests and custom scripts. Each entry accepts `name`, `path`, optional
  `args`, optional `env`, optional `python`, and `timeout_sec`.
- `ssh`: Host-side SSH checks run after smoke tests. Each entry accepts `name`,
  `command`, optional `expect`, optional `host` (falls back to `dut.network.address`),
  `user`, `port`, `identity_file`, `ssh_binary`, `extra_args`, and `timeout_sec`.
- `command_timeout_sec`: Timeout for each smoke command.

Every smoke/script/pytest/SSH entry accepts `enabled` (default `true`). Set
`enabled: false` to keep the entry documented in config while recording it as skipped.
Any failed smoke, script, pytest, or SSH result marks the job failed; reports keep all
post-upgrade result sections so the failure evidence is still available.

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
  ap-be5000:
    builder:
      command: [make, owrt2102.asus_eap5000_mt7987]
    artifact:
      patterns:
        - build/owrt2102/bin/target/openwrt-*-ASUS-EAP5000-squashfs-sysupgrade.bin
    upgrade:
      transfer: tftp
      host_interface: bridge100
```

`--profile` is accepted by `validate`, `dry-run`, `build`, `run`, `flash`, `test`, `resume`.
If omitted, `project.default_profile` is used when configured. The applied profile name is
captured in the per-job provenance (`build_metadata.profile`).
