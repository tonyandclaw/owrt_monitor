# Safe Firmware Upgrade

The `sysupgrade -n` step is destructive: a half-flashed image can brick a board. This guide
is the checklist to run before pressing the real button.

## Why every step exists

Each guard exists because something went wrong at least once. They're cheap to honour and
expensive to skip.

## Before you flash

1. **Confirm the right artifact for the right DUT.**
   - BE5000 AP boards take the ASUS EAP5000 eMMC sysupgrade image, not generic
     `mediatek_mt7987a-spim-nand`.
   - Other boards have other suffixes; flashing the wrong variant is a common cause of
     boot failures even when the build itself succeeds.
   - The `artifact.patterns` glob in your active profile should be specific enough to
     return exactly one candidate. `selection: newest` is a safety net, not a substitute
     for a tight pattern.

2. **Validate the config.**

   ```sh
   owrt-monitor validate --config configs/example.yaml --profile <name>
   ```

3. **Dry-run the full plan.**

   ```sh
   owrt-monitor run --config configs/example.yaml --profile <name> \
       --dry-run --allow-flash
   ```

   Read the resulting `report.md`. It will show, in order:
   - Build command
   - Artifact search patterns
   - DUT lock + serial console
   - Firmware transfer command (`tftp -g -r ...`, `wget -O ...`, `scp ...`, or custom host command)
   - Upgrade command (`sysupgrade -n /tmp/firmware.bin`)
   - Smoke tests

   If any line surprises you, fix the config before running for real.

4. **Confirm the DUT is the one you intend to flash.**
   - Serial port path matches.
   - DUT has the expected hostname / model. (Run `ubus call system board` over a
     manual serial session if you're not sure.)

5. **Confirm `/tmp` headroom on the DUT.**
   - Set `upgrade.min_dut_free_kb` to at least the firmware size; the workflow will
     pre-check before transferring.

6. **For TFTP transfer:** confirm `/private/tftpboot` is writable. For
   `bootloader_tftp`, also confirm the host `tftpd` service is running on UDP/69.

   ```sh
   sudo launchctl list com.apple.tftpd
   ls -ld /private/tftpboot
   ```

   **For SCP transfer:** confirm SSH/SCP login works non-interactively. On OpenWrt
   Dropbear targets without SFTP, add `scp_extra_args: ["-O"]`.

   **For custom transfer:** run the configured command manually once against a harmless
   file and confirm it writes to `upgrade.remote_path`.

## Running the real flash

```sh
owrt-monitor run --config configs/example.yaml --profile <name> --allow-flash
```

What this does, in order:

1. Acquires the DUT lock (sqlite-backed, with stale-recovery after `dut.lock_timeout_sec`).
2. Opens the serial console and waits for the prompt.
3. Publishes the firmware: copies into `tftp_root` and starts a temporary TFTP server
   for shell TFTP, starts a temporary HTTP server for HTTP transfers, runs `scp`, or
   runs `custom_transfer_command`.
4. Tells the DUT to fetch the firmware (`tftp -g` or `wget`), unless SCP/custom host
   command handles transfer.
5. Verifies the size, and (if `verify_sha256: true`) the SHA256 hash, on the DUT side.
6. Runs the configured upgrade command (`sysupgrade -n /tmp/firmware.bin` by default).
7. Waits for the prompt to return — fails fast on any `boot_failure_patterns` regex.
8. Applies any configured `upgrade.post_upgrade_network` normalization, such as forcing
   `br-lan` back to DHCP after the upgraded image boots.
9. Runs configured smoke tests.
10. Releases the DUT lock.

If any configured smoke, custom script, pytest, or SSH test fails, the job is marked
`FAILED` and the report keeps the failing output section for triage.

## If you need to abort mid-flash

```sh
owrt-monitor cancel <job_id>
```

Writes a marker file the workflow polls between steps. Cancellation lands the job in
`CANCELLED` state, not `FAILED`, so it's distinguishable from real failures.

`sysupgrade` itself is not interruptible once running — cancellation will take effect at
the next state boundary (typically post-reboot-wait). If the workflow process is wedged on
a non-cancellable read, the recorded PID is in `owrt-monitor status`; kill it manually as
a last resort.

## After a failure

The run directory is never deleted. You always have:

- `config.snapshot.yaml` — the exact config used (post-profile-merge, secrets redacted)
- `events.jsonl` — every state transition with timestamp
- `build.log` — full make output
- `serial.log` — full serial transcript including the boot stream
- `report.json` / `report.md` — structured + human-readable summary
- `firmware/<filename>.bin` — exactly the bytes that went to the DUT (with SHA256 on disk)

Use `owrt-monitor resume <job_id> --allow-flash --profile <name>` to retry just the flash
phase if the build is still good. See `docs/troubleshooting.md` for failure-specific
recovery.
