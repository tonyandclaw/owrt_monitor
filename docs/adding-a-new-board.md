# Adding a New Board

How to bring a new DUT under `owrt-monitor` control. The mechanical work is one new
`profiles:` entry in your config.

## What to gather first

Before writing any config, collect:

| Item | Where to find it |
| --- | --- |
| The make target name | `ls /home/asus/openwrt/enw-device-firmware/conf/` inside the builder container |
| The build subdir | The `<framework>` prefix of the make target (`owrt2102.foo` → `build/owrt2102/`) |
| Output filename pattern | After a manual build: `ls build/<framework>/bin/target/openwrt-*-sysupgrade.bin` |
| Serial port path on host | `ls /dev/cu.usbserial-* /dev/cu.usbmodem*` with the DUT plugged in |
| Serial baud | Vendor docs (often 115200) |
| Shell prompt regex | Connect manually with `screen` / `picocom` and observe |
| DUT IP | `ip a` once it's booted |
| Reachable host interface/IP from the DUT | Host interface/service name such as `bridge100`, or a fixed host IP |

## Add the profile

In `configs/example.yaml` (or your own config), append to `profiles:`:

```yaml
profiles:
  newboard:
    builder:
      command: [make, owrt2102.your_make_target]
    artifact:
      # Be specific. AP-class profiles emit ~17 variants; pin the right one.
      patterns:
        - build/owrt2102/bin/target/openwrt-*-your_board-sysupgrade.bin
    upgrade:
      transfer: tftp                    # monitor-managed shell TFTP
      tftp_root: /private/tftpboot      # default
      tftp_port: 0                      # auto high port
      host_interface: bridge100
      # Or, if the host IP is truly static:
      # tftp_host: 192.168.1.66
      command: sysupgrade -n /tmp/firmware.bin
    dut:
      name: newboard-01
      serial: /dev/cu.usbserial-XYZ
      prompt: "root@OpenWrt:.*# "
      network:
        address: 192.168.1.1
    tests:
      smoke:
        - ubus call system board
        - cat /proc/version
        - /etc/init.d/network status
```

The deep-merge keeps everything not overridden inheriting from the base config. Override
only what's actually different per board.

## Verify

```sh
# Schema + profile validity:
owrt-monitor validate --config configs/example.yaml --profile newboard

# Plan everything (no docker, no DUT):
owrt-monitor dry-run --config configs/example.yaml --profile newboard

# Plan including DUT actions, still no side effects:
owrt-monitor run --config configs/example.yaml --profile newboard \
    --dry-run --allow-flash
```

If the dry-run report shows the right `make` target and the right `tftp -g` /
`wget` / `sysupgrade` lines, the profile is wired correctly.

## First real build

```sh
owrt-monitor build --config configs/example.yaml --profile newboard
```

Look at `report.md`:

- `Classification: success` and a duration → the build worked.
- The artifact filename + SHA256 → that's exactly what you'll flash.
- The provenance section (git_commit, git_describe, git_dirty) → traceable to a tree state.

If `Classification: failed_package` shows up, the per-package gap is named directly. See
`docs/troubleshooting.md`.

## First real flash

Only after all of the above is clean:

```sh
owrt-monitor run --config configs/example.yaml --profile newboard --allow-flash
```

See `docs/safe-upgrade.md` for the pre-flash checklist.

## Tightening the profile over time

- Once you observe the actual board's boot transcript on serial, add board-specific
  lines to `upgrade.boot_failure_patterns` if you see fault signatures the defaults miss.
- Add board-specific smoke tests once you know what's interesting to assert (an
  expected interface up, a specific package installed, etc.).
- If multiple operators use the same lab, raise `dut.lock_timeout_sec` to match the
  longest expected boot sequence so concurrent jobs don't break each other's locks.
