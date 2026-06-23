# Lab Setup

This guide covers what the host needs before `owrt-monitor build` and `owrt-monitor run` can
talk to a real OpenWrt builder container and a real DUT.

## Host requirements

- macOS (the project's primary target — Linux works too but isn't the daily-driver platform).
- Docker Desktop or equivalent. The build image (`build_dev:2.0-claude` in this lab) has to
  contain the OpenWrt build dependencies plus `bash`, `stat`, `git`, and `df` (used by
  `DockerBuildClient`'s preflight + artifact detector).
- Python 3.11+ (`python3 -m pip install -e ".[dev,serial]"`).
- A USB serial cable to the DUT (e.g. `/dev/cu.usbserial-0001` on macOS).
- A writable TFTP root such as `/private/tftpboot/` if you use `transfer: tftp`.
  OpenWrt-shell TFTP is served by a temporary `owrt-monitor` process on a high port, so
  macOS `tftpd` is not required for that path. Bootloader recovery (`bootloader_tftp`)
  still needs a normal TFTP service on UDP/69:

  ```sh
  sudo mkdir -p /private/tftpboot
  sudo chmod 755 /private/tftpboot
  sudo launchctl load -F /System/Library/LaunchDaemons/tftp.plist
  sudo launchctl start com.apple.tftpd
  ```

## Builder container

Entered via the lab's `/usr/local/bin/enter_docker.sh` (idempotent: re-uses if running, starts
if stopped, creates if missing). Layout it expects:

| Item | Path |
| --- | --- |
| Container name | `openwrtbuild` |
| Image | `build_dev:2.0-claude` |
| Inside-container user | `asus` |
| Firmware tree root | `/home/asus/openwrt/enw-device-firmware` |
| Profile definitions | `/home/asus/openwrt/enw-device-firmware/conf/` |
| Per-profile build root | `/home/asus/openwrt/enw-device-firmware/build/<framework>/` |
| Sysupgrade output | `<build_root>/bin/target/openwrt-*-sysupgrade.bin` |
| Download cache | `/home/asus/openwrt-dl` (named volume — survives container recreation) |

The build is `make <profile>` from the firmware root. Known profiles:

| Profile | Board class |
| --- | --- |
| `owrt2102.asus_eap5000_mt7987` | BE5000 AP (default `ap-be5000`) |
| `owrt2102.asus_eap14000_mt7987` | BE14000 AP |
| `owrt2102.asus_controller_mt7988a` | controller (MT7988A) |
| `owrt2102.asus_gw_mt7988` | gateway (MT7988) |
| `owrt2410.asus_mt76_mt7987` | AP variant using mt76 driver |
| `owrt2512.asus_microchipsw` | switch (Microchip) |

## DUT

For the default BE5000 AP profile in this lab:

- Reachable serial console (USB-to-UART).
- Reachable network from the DUT to the host interface configured by `upgrade.host_interface`
  (default `bridge100` for TFTP).
- `tftp` client present on the OpenWrt shell (BusyBox default — yes).
- Enough free space in `/tmp` for the firmware (~30 MB for AP).

## Disk hygiene

The OpenWrt build is bandwidth- and disk-hungry. Container overlay around 20–30 GB is
typical when build_dir is warm; see `docs/troubleshooting.md` for the recovery recipe when
you hit `No space left on device`.

`builder.min_free_disk_mb` (default 5000 MB) preflight-fails the build before it starts if
the workdir filesystem is below threshold — saves you 30+ minutes of make output that ends
in a `prepare-tmpinfo` error.

## Sanity checks before flashing

```sh
# Validate the active profile's config:
owrt-monitor validate --config configs/example.yaml

# Plan everything without side effects (no docker, no DUT serial/network touch):
owrt-monitor dry-run --config configs/example.yaml

# Plan the full flow including DUT actions:
owrt-monitor run --config configs/example.yaml --dry-run --allow-flash
```

`lab-check` opens the configured serial port briefly, sends a newline, and
waits for `dut.prompt`. If the dry-run report.md and lab-check look right, swap
`--dry-run` for the real run.
