from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from owrt_monitor.config import BuilderConfig
from owrt_monitor.docker_build import DockerBuildClient, DockerBuildError


def _builder(min_free_disk_mb: int = 5000) -> BuilderConfig:
    return BuilderConfig(
        container="fake",
        workdir="/work/openwrt",
        command=["make"],
        min_free_disk_mb=min_free_disk_mb,
    )


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_disk_preflight_passes_when_enough_space() -> None:
    client = DockerBuildClient(_builder(min_free_disk_mb=100))
    # 1 GB available, threshold 100 MB → pass.
    one_gb = 1024 * 1024 * 1024
    df_output = f"Avail\n{one_gb}\n"
    with patch("subprocess.run", return_value=_completed(df_output)):
        client._preflight_disk_space()


def test_disk_preflight_raises_when_below_threshold() -> None:
    client = DockerBuildClient(_builder(min_free_disk_mb=5000))
    # 100 MB available, threshold 5000 MB → fail.
    df_output = f"Avail\n{100 * 1024 * 1024}\n"
    with patch("subprocess.run", return_value=_completed(df_output)):
        with pytest.raises(DockerBuildError, match=r"insufficient disk"):
            client._preflight_disk_space()


def test_disk_preflight_skipped_when_threshold_zero() -> None:
    client = DockerBuildClient(_builder(min_free_disk_mb=0))
    # subprocess.run must NOT be called when the check is disabled.
    with patch("subprocess.run") as run:
        client._preflight_disk_space()
        run.assert_not_called()


def test_disk_preflight_skipped_when_df_introspection_fails() -> None:
    client = DockerBuildClient(_builder(min_free_disk_mb=5000))
    # df returned non-zero (e.g. busybox without --output) — don't block the build.
    with patch("subprocess.run", return_value=_completed(returncode=1)):
        client._preflight_disk_space()


def test_disk_preflight_skipped_when_df_output_unparseable() -> None:
    client = DockerBuildClient(_builder(min_free_disk_mb=5000))
    with patch("subprocess.run", return_value=_completed("not a number\n")):
        client._preflight_disk_space()
