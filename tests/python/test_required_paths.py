from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
from owrt_monitor.config import BuilderConfig
from owrt_monitor.docker_build import DockerBuildClient, DockerBuildError


def _builder(required: list[str]) -> BuilderConfig:
    return BuilderConfig(
        container="fake",
        workdir="/work/openwrt",
        command=["make"],
        required_paths=required,
    )


def _completed(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr="")


def test_skipped_when_required_paths_empty() -> None:
    client = DockerBuildClient(_builder([]))
    with patch("subprocess.run") as run:
        client._preflight_required_paths()
        run.assert_not_called()


def test_passes_when_all_paths_exist() -> None:
    client = DockerBuildClient(_builder(["feeds.conf", "package/feeds/mtk"]))
    with patch("subprocess.run", return_value=_completed(returncode=0)):
        client._preflight_required_paths()


def test_raises_when_any_path_missing() -> None:
    client = DockerBuildClient(_builder(["feeds.conf", "missing/path"]))
    # Two test calls; first succeeds, second fails.
    return_values = [_completed(returncode=0), _completed(returncode=1)]
    with patch("subprocess.run", side_effect=return_values):
        with pytest.raises(DockerBuildError, match=r"required paths missing.*missing/path"):
            client._preflight_required_paths()


def test_collects_all_missing_paths() -> None:
    """Error message should list every missing path, not bail on the first."""
    client = DockerBuildClient(_builder(["a", "b", "c"]))
    with patch("subprocess.run", return_value=_completed(returncode=1)):
        with pytest.raises(DockerBuildError, match=r"\['a', 'b', 'c'\]"):
            client._preflight_required_paths()
