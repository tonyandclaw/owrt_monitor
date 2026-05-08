from __future__ import annotations

import subprocess
import sys

from owrt_monitor import __version__


def test_version_constant_exposed() -> None:
    assert isinstance(__version__, str)
    assert __version__.count(".") >= 2  # MAJOR.MINOR.PATCH at minimum
    parts = __version__.split(".")[:3]
    for part in parts:
        # Allow numeric or numeric+suffix (e.g. "0.1.0a1") on the patch slot.
        assert part[0].isdigit(), f"version part {part!r} must start with a digit"


def test_version_flag_prints_and_exits_zero() -> None:
    """`owrt-monitor --version` prints `owrt-monitor <ver>` and exits 0."""
    result = subprocess.run(
        [sys.executable, "-m", "owrt_monitor", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert f"owrt-monitor {__version__}" in result.stdout


def test_short_version_flag_works() -> None:
    """`-V` short flag is equivalent to `--version`."""
    result = subprocess.run(
        [sys.executable, "-m", "owrt_monitor", "-V"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert f"owrt-monitor {__version__}" in result.stdout
