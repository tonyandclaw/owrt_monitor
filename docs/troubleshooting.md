# Troubleshooting

What to look at when a job lands in `FAILED` or `CANCELLED` state.

## First step always

```sh
owrt-monitor status --config configs/example.yaml
```

Find the failed job's `Run Dir` and read `report.md`. The build-log classifier and provenance
metadata mean most failures point at their own root cause without needing to grep `build.log`.

```sh
cat artifacts/<job_id>/report.md
```

If the job process itself died (PID column shows `no` for a non-terminal job), that's an
orphan — see "Orphan jobs" below.

## Build failures

### `Classification: disk_full`

**Signature lines** (visible in `report.md`'s evidence section):

- `awk: ... fatal: print to "standard output" failed: No space left on device`
- `Unable to flush stdout: No space left on device`
- `make[N]: *** [...prepare-tmpinfo] Error 1`

**Cause.** Container overlay or `/tmp` is full mid-build. OpenWrt's `make` writes a lot of
intermediate state in `tmp/` early.

**Recovery.**

```sh
# How much is reclaimable?
docker system df

# Free up the easy stuff first (won't touch running containers):
docker image prune -a            # delete dangling images
docker builder prune -af         # delete unused build cache

# If still tight, look for stale tagged backups:
docker images
docker rmi <name>:<tag>          # explicit per-image
```

`builder.min_free_disk_mb` (default 5000 MB) catches the most extreme cases at preflight so
you fail in 1 second instead of 30 minutes. Tighten the threshold per profile if you see
disk-full happening with more than 5 GB free.

### `Classification: failed_package`

`report.md` shows `Failed package: <foo/bar>` and `Failed step: <package/.../compile>`.

**Cause.** A specific OpenWrt package failed to compile. Common reasons:

- Missing kernel symbol — compare `build/<framework>/.config` against `package/.../config`.
- Feed/`feeds.conf` drift after a tree update — try `./scripts/feeds update -a && ./scripts/feeds install -a` inside the container.
- Stale `build_dir` for that package — remove `build/<framework>/build_dir/<package>*` and rebuild.

The full `build.log` in the run directory is usually 200–2000 lines for a package failure
and ends with the failing `gcc`/`ld` line.

### `Classification: compile_error`

Generic `make: *** [...] Error N` with no specific package failure underneath. Often means
the failure happened during `target/install` or one of OpenWrt's `Makefile` glue steps.
Look at the last ~100 lines of `build.log`.

### `build timed out after N seconds`

`builder.timeout_sec` was non-zero and exceeded. The partial `build.log` is preserved and
the build classifier still runs against it. Increase `builder.timeout_sec` (or set to `0` for
unbounded) and rerun.

## DUT / flash failures

### `DUT failed to boot after upgrade: Kernel panic - not syncing: ...`

The reboot-wait detected a kernel panic line during boot and aborted instead of waiting out
`upgrade.boot_timeout_sec`. Read `serial.log` in the run dir for the panic context.

Common causes:

- Wrong board variant flashed (eMMC image to a NAND board, etc.) — check the artifact
  filename matches the actual hardware.
- Bad sysupgrade — recover via the bootloader's TFTP / failsafe mode (board-specific).
- Truly broken firmware — bisect via git.

### `DUT <name> is already locked`

A prior job crashed while holding the DUT lock. If `dut.lock_timeout_sec` (default 1800 s)
hasn't elapsed, wait or bump it; otherwise the lock will be auto-broken on next acquire.
Stale locks from killed processes self-recover on the next attempt.

### `tftp` command on the DUT exits non-zero

Check from the host:

```sh
# Is tftpd running?
sudo launchctl list com.apple.tftpd

# Did the firmware actually publish?
ls -l /private/tftpboot/

# Permissions readable by tftpd?
ls -ld /private/tftpboot/
```

The published file should be world-readable. `tftpd` runs as `nobody` by default.

### Refusing to flash without `--allow-flash`

Intentional safety. The destructive `sysupgrade` command requires the explicit flag because
running it against the wrong DUT is hard to recover from. Keep the gate.

## Orphan jobs

`owrt-monitor status` shows `Alive: no` for a non-terminal job → the workflow process died
without persisting a final state. The job's `run_dir` is intact and the DUT lock will
self-recover after `dut.lock_timeout_sec`. To clean up the DB record manually:

```sh
sqlite3 artifacts/owrt_monitor.sqlite3 \
    "UPDATE jobs SET state='FAILED', result='orphan', \
                     finished_at=datetime('now') WHERE id='<job_id>'"
```

Then `owrt-monitor cancel <job_id>` to write the marker (paranoia — in case the process
rises from the dead).

## Resuming a partially-completed job

If the build completed but the flash failed, the build artifact is reusable:

```sh
owrt-monitor resume <job_id> --allow-flash --profile <name>
```

Supported resume points: `BUILD_SUCCEEDED`, `ARTIFACT_SELECTED`, `ARTIFACT_EXPORTED`. DUT-phase
resume is intentionally not supported because device state after a partial flash is
undetermined.
