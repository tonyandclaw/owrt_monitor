# owrt_monitor Architecture

## Goal

`owrt_monitor` 的目標是在 macOS host 上穩定完成這條流程：

1. 依照設定選擇 OpenWrt build container。
2. 在 container 裡執行指定 build command。
3. 偵測是否產出 firmware artifact。
4. 用 `docker cp` 或等效方式把指定 firmware 匯出到 host。
5. 透過 USB serial 控制 DUT。
6. 讓 DUT 取得 firmware，執行 upgrade。
7. 等待 DUT reboot，確認系統恢復。
8. 執行 smoke/integration tests。
9. 保存 log、metadata、test report，必要時交給 LLM 做輔助分析。

這套系統的核心要求是穩定、可恢復、可觀測、可重跑。iTerm2 tab 可以用來看 log，但不應該成為流程控制的核心。

## High-Level Architecture

```text
                 +----------------------+
                 | YAML/JSON Config     |
                 +----------+-----------+
                            |
                            v
                 +----------------------+
                 | Python Orchestrator  |
                 | workflow/state/test  |
                 +----------+-----------+
                            |
              local API / JSONL / gRPC
                            |
                            v
                 +----------------------+
                 | Go Runner / owrtd    |
                 | process/log/locks    |
                 +----+------------+----+
                      |            |
          Docker exec |            | Serial/host process
                      |            |
                      v            v
        +------------------+   +-------------------+
        | OpenWrt Builder  |   | DUT over USB      |
        | Docker Container |   | Serial Console    |
        +--------+---------+   +---------+---------+
                 |                       |
                 v                       v
        +------------------+   +-------------------+
        | Firmware Artifact|   | Upgrade + Tests   |
        +--------+---------+   +---------+---------+
                 |                       |
                 +-----------+-----------+
                             v
                 +----------------------+
                 | Reports / Logs / DB  |
                 +----------------------+
```

## Language Split

### Python Responsibilities

Python is the control brain:

- Parse and validate YAML config。
- Own the workflow state machine。
- Provide user-facing CLI。
- Implement board/profile-specific behavior。
- Run test scripts and pytest suites。
- Parse OpenWrt build logs and DUT boot logs。
- Coordinate artifact metadata。
- Generate reports。
- Integrate optional LLM analysis。

Python is intentionally favored for fast iteration because OpenWrt labs often need per-board quirks, custom shell commands, and test experiments.

Recommended Python libraries:

- `pydantic` or `attrs` for config validation。
- `typer` or `click` for CLI。
- `pyserial` for serial。
- `pexpect` for prompt-like interactions。
- `docker` SDK or controlled `subprocess` calls for Docker。
- `pytest` for post-upgrade tests。
- `sqlite3` or SQLAlchemy for job state。
- `rich` for local CLI output。

### Go Responsibilities

Go is the stable execution engine:

- Long-running daemon。
- Process supervision。
- Log streaming。
- Job cancellation。
- Timeout enforcement。
- Resource locks。
- Local API。
- Crash-resilient heartbeat。
- Serial streaming if Python serial handling becomes unstable。

Go should avoid board-specific business logic where possible. It should expose reliable primitives that Python can compose.

Recommended Go packages:

- Standard `os/exec` and `context` for process control。
- `net/http` or gRPC for local API。
- `encoding/json` for JSONL events。
- `go.bug.st/serial` for serial support if moved into Go。
- SQLite driver if daemon owns persistent state。

## iTerm2 and tmux Role

iTerm2/tmux should be an observer layer, not the automation engine.

Allowed responsibilities:

- Open panes/tabs for build logs。
- Show serial console。
- Show job status。
- Attach to a running session。
- Help manual debugging。

Avoid these as core behavior:

- Relying on AppleScript to type commands into iTerm tabs。
- Parsing iTerm screen text as the source of truth。
- Treating a tab crash as a workflow crash。

The reliable source of truth should be structured job state, logs, and runner events.

## Core Concepts

### Job

A job is one complete build/flash/test execution. It has:

- job id。
- config snapshot。
- current state。
- timestamps。
- resource locks。
- logs。
- artifact metadata。
- final result。

### Build

A build runs inside a configured Docker container. The build step records:

- container id/name。
- command。
- working directory。
- environment。
- stdout/stderr。
- exit code。
- duration。

### Artifact

An artifact is a firmware file selected from the build output. It records:

- container path。
- host path。
- filename。
- size。
- SHA256。
- OpenWrt target/profile if detectable。
- build job id。

### DUT

A DUT is a physical device reachable through USB serial and optionally network. It records:

- name。
- serial path。
- baud rate。
- prompt regex。
- login method。
- network address or discovery method。
- upgrade method。

### Test Run

A test run is a post-upgrade validation pass. It records:

- test profile。
- commands/scripts。
- pass/fail/skipped。
- output。
- duration。

## Suggested Repository Layout

```text
owrt_monitor/
  README.md
  TODO.md
  ARCHITECTURE.md
  configs/
    example.yaml
  cmd/
    owrtd/
      main.go
    owrtctl/
      main.go
  internal/
    runner/
    locks/
    api/
    logs/
  python/
    owrt_monitor/
      __init__.py
      cli.py
      config.py
      workflow.py
      docker_build.py
      artifacts.py
      dut_serial.py
      upgrade.py
      tests.py
      reports.py
      llm_analysis.py
  tests/
    python/
    go/
    fixtures/
  docs/
    quickstart.md
    config-reference.md
    lab-setup.md
```

## Configuration Model

Example shape:

```yaml
project:
  name: owrt-monitor-lab
  artifact_dir: ./artifacts

builder:
  container: openwrt-builder
  workdir: /work/openwrt
  command:
    - make
    - -j8
  env:
    FORCE_UNSAFE_CONFIGURE: "1"

artifact:
  patterns:
    - bin/targets/**/**/openwrt-*-sysupgrade.bin
  selection: newest
  min_size_mb: 4
  require_sha256: true

dut:
  name: dut-01
  serial: /dev/cu.usbserial-0001
  baud: 115200
  prompt: "root@OpenWrt:.*# "
  login:
    username: root
  network:
    address: 192.168.1.1
    interface: br-lan

upgrade:
  transfer: http
  remote_path: /tmp/firmware.bin
  command: sysupgrade -n /tmp/firmware.bin
  boot_timeout_sec: 240

tests:
  smoke:
    - ubus call system board
    - opkg list-installed
    - /etc/init.d/network status
```

## Workflow State Machine

```text
PENDING
  -> PREFLIGHT
  -> BUILD_RUNNING
  -> BUILD_SUCCEEDED
  -> ARTIFACT_SELECTED
  -> ARTIFACT_EXPORTED
  -> DUT_LOCKED
  -> DUT_READY
  -> FIRMWARE_TRANSFERRED
  -> UPGRADE_RUNNING
  -> REBOOT_WAIT
  -> DUT_ONLINE
  -> TEST_RUNNING
  -> SUCCEEDED

Any state can move to:
  -> FAILED
  -> CANCELLED
```

Every state transition should be written to persistent storage before external side effects continue. This makes crash recovery possible.

## Build Flow

1. Validate config。
2. Confirm Docker is available。
3. Confirm configured container exists and is running。
4. Confirm OpenWrt workdir exists in container。
5. Execute build command through the runner。
6. Stream build logs to file and terminal observers。
7. Classify build result。
8. Search artifact patterns inside container。
9. Select artifact using configured policy。
10. Copy artifact to host artifact directory。
11. Generate SHA256 and metadata。

The build flow should not depend on iTerm2. iTerm2 may subscribe to logs.

## Firmware Transfer Flow

Preferred first implementation: host HTTP server.

1. Start a temporary HTTP server on host。
2. Expose only the selected firmware directory。
3. Use serial shell to run DUT download command:

```sh
wget -O /tmp/firmware.bin http://HOST_IP:PORT/firmware.bin
```

4. Verify file size on DUT。
5. Verify SHA256 when available。
6. Stop HTTP server after transfer or job completion。

This is usually more predictable than trying to push a large firmware through serial.

## Upgrade Flow

1. Lock DUT。
2. Confirm serial prompt。
3. Confirm DUT model/board if configured。
4. Confirm `/tmp` free space。
5. Download firmware。
6. Verify checksum。
7. Execute upgrade command。
8. Keep reading serial logs while the device reboots。
9. Detect boot start and boot completion。
10. Wait for shell prompt。
11. Run post-upgrade tests。

For OpenWrt, the default upgrade command should be:

```sh
sysupgrade -n /tmp/firmware.bin
```

Board-specific override must be configurable.

## Serial Session Design

Serial handling needs to be defensive:

- Read continuously in a background loop。
- Timestamp every line/chunk。
- Support binary/noisy boot output。
- Do not assume every command has a clean echo。
- Use regex prompt detection with timeout。
- Save complete transcript。
- Distinguish command timeout from serial disconnect。
- Allow reconnect after reboot。

Serial prompt interactions should be built as explicit expect steps, not arbitrary sleeps.

## Resource Locking

Locks prevent accidental concurrent destructive operations.

Required locks:

- DUT lock: only one job can flash a DUT。
- serial port lock: only one process can open a serial port。
- artifact path lock: avoid overwriting firmware outputs。
- container lock: optional, useful when one builder container cannot handle parallel builds。

Locks should include:

- owner job id。
- created timestamp。
- heartbeat timestamp。
- stale lock policy。

Current implementation keeps Python's SQLite-backed DUT/builder locks for the
daily-driver workflow and writes `<artifacts_dir>/locks.json` after each
mutation. owrtd reads that snapshot and can also mutate Go-owned DUT,
builder/container, serial, and artifact locks through the local `/v1/locks/...`
API using the same JSON fields.

## Persistence

Recommended first version: SQLite.

Tables:

- `jobs`。
- `job_events`。
- `artifacts`。
- `dut_locks`。
- `test_results`。
- `llm_reports`。

Store config snapshots with each job so future debugging does not depend on the current config file.

## Logging and Reports

Each job should produce:

```text
artifacts/<job-id>/
  config.snapshot.yaml
  build.log
  serial.log
  upgrade.log
  test.log
  artifact.json
  report.json
  report.md
```

Log levels:

- DEBUG: low-level command details。
- INFO: state transitions。
- WARN: retryable issues。
- ERROR: failed steps。
- EVENT: structured machine-readable events。

Use JSONL for machine parsing and Markdown/console summary for humans.

## LLM Boundary

LLM assistance should never be the authority for firmware flashing.

Safe LLM tasks:

- Build log summarization。
- Error classification。
- Suggesting likely failing package。
- DUT boot failure summary。
- Drafting troubleshooting notes。

The implemented advisory path is `owrt-monitor analyze <job_id|run_dir>`.
It reads only persisted job artifacts, redacts common secret-shaped tokens,
keeps source file hashes and line references, and writes `analysis.json` plus
`analysis.md` into the run directory. The analysis includes deterministic
next-action suggestions and a redacted bug report draft. This file is
structured input for a future LLM/UI layer; it is not an execution authority.

Unsafe LLM tasks unless explicitly approved:

- Choosing firmware file。
- Running `sysupgrade`。
- Deleting build directories。
- Modifying DUT bootloader environment。
- Changing network settings on DUT。

The deterministic workflow and config remain the source of truth.

## API Between Python and Go

MVP can use direct Python subprocess calls. Long term, Python should call Go daemon API.

Possible local API:

```http
GET  /ui/
POST /v1/jobs
GET  /v1/jobs/{id}
GET  /v1/jobs/{id}/analysis
GET  /v1/jobs/{id}/runner
GET  /v1/jobs/{id}/runner-output
POST /v1/jobs/{id}/cancel
GET  /v1/jobs/{id}/events
GET  /v1/locks
POST /v1/locks/{dut|builder|serial|artifact}/{name}/acquire
POST /v1/locks/{dut|builder|serial|artifact}/{name}/heartbeat
POST /v1/locks/{dut|builder|serial|artifact}/{name}/release
```

Current `POST /v1/jobs` bridge:

```json
{
  "command": "run",
  "config": "configs/example.yaml",
  "profile": "ap",
  "dry_run": true,
  "allow_flash": true
}
```

Python CLI commands (`build`, `run`, `flash`, `test`, and `dry-run`) can submit
through this API with `--daemon-url http://127.0.0.1:8765`. Without that flag,
they keep the direct Python-only fallback path.

Allowed `command` values are `build`, `run`, `flash`, and `test`. For `flash`,
`artifact` is required, and destructive runs require `allow_flash: true`. owrtd
generates a `job_<hex>` id, sets `OWRT_MONITOR_JOB_ID` for the Python child
process, writes human-readable child output to `<run_dir>/runner.log`, writes
structured stdout/stderr records to `<run_dir>/runner.output.jsonl`, and
maintains `<run_dir>/runner.json` with the child pid, command, status,
timestamps, and exit code. While the child is running, owrtd refreshes
`updated_at` as a heartbeat. `GET /v1/jobs/{id}/runner` returns that runner
status. If an active runner status points at a dead PID after daemon restart,
owrtd marks it `orphaned` and persists `orphaned_at`. `GET
/v1/jobs/{id}/runner-output` streams the structured output as
`application/x-ndjson`. Add `?tail=N` to return only the last N records, or
`?follow=true` to keep streaming until the runner exits or the client
disconnects.
`--runner-output-rotate-bytes` bounds the active `runner.log` +
`runner.output.jsonl` segment size (default 16MiB combined), and
`--runner-output-rotate-files` controls how many rotated segments are kept
(default 3). Rotation moves both files together through `.1`, `.2`, `.3`, and
the runner-output API reads rotated segments from oldest to newest before the
current file. When rotation happens, owrtd sets
`runner.json.output_rotated=true`.
`--runner-output-max-bytes` bounds per-job runner output written by owrtd; when
the limit is reached, owrtd keeps draining stdout/stderr, writes one truncation
marker, discards later lines, and sets `runner.json.output_truncated`.
`POST /v1/jobs/{id}/cancel` writes the same cooperative `cancel.flag` marker
used by the Python CLI and, for owrtd-launched jobs, marks `runner.json` as
`cancel_requested` with `cancel_requested_at`.
`GET /v1/locks` returns the lock snapshot from `<artifacts_dir>/locks.json`.
The Go lock mutation API uses that same snapshot shape and supports
`dut`, `builder`/`container`, `serial`, and `artifact` locks. Acquire requests
accept `owner_job_id` plus optional `lock_timeout_sec`; active locks return
409, stale locks are replaced, heartbeat refreshes `heartbeat_at`, and release
requires the matching owner.
`GET /v1/jobs?limit=N` sorts by report `started_at` when present, and falls
back to `report.json` mtime for older/dry-run reports that do not carry a
timestamp.
`GET /ui/` is a read-only dashboard served by owrtd itself. It consumes the
same local API and intentionally exposes no submit, cancel, or flash buttons.

Event format:

```json
{
  "ts": "2026-05-08T10:15:30+08:00",
  "job_id": "job_abc123",
  "level": "INFO",
  "component": "builder",
  "event": "build_started",
  "message": "OpenWrt build started",
  "fields": {
    "container": "openwrt-builder"
  }
}
```

## Failure Handling

Common failures and expected behavior:

- Docker container missing: fail during preflight。
- Build command fails: save logs, classify error, do not flash。
- Artifact missing: fail after build, do not flash。
- Multiple artifacts: follow selection policy or fail if ambiguous。
- DUT serial missing: fail before destructive action。
- DUT prompt timeout: retry reconnect, then fail。
- Firmware checksum mismatch: delete remote firmware, do not upgrade。
- Upgrade command starts but boot never returns: mark failed, preserve serial log。
- Tests fail: mark job failed but preserve upgraded state note。

## Security and Safety

- Never log secrets。
- Require explicit config for destructive commands。
- Default to `sysupgrade -n` only when configured board/profile matches。
- Require checksum after artifact copy。
- Avoid shell string concatenation when possible。
- Prefer argument arrays for commands。
- Keep LLM output advisory。
- Support dry-run for command preview。

## Implementation Strategy

Recommended sequence:

1. Python-only vertical slice:
   - config。
   - Docker build。
   - artifact export。
   - serial connect。
   - manual-safe upgrade command。
   - smoke tests。
2. Add persistence and resumability。
3. Add fake DUT and fake Docker integration tests。
4. Extract process runner and log streaming into Go。
5. Add daemon API and locks。
6. Add tmux/iTerm observer。
7. Add LLM log analysis。
8. Harden for multiple DUTs and scheduled jobs。

This lets the project become useful early without locking the design too soon.
