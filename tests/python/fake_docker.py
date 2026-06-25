from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from owrt_monitor.artifacts import ArtifactCandidate, ExportedArtifact
from owrt_monitor.cancel import CancelToken
from owrt_monitor.config import BuilderConfig
from owrt_monitor.docker_build import DockerBuildError, sha256_file


@dataclass
class FakeDockerBuildClient:
    """A drop-in replacement for DockerBuildClient that fabricates artifacts.

    Designed for end-to-end workflow tests: simulates the build by writing a
    canned build.log, then synthesises a single artifact file when asked.
    Set `build_should_fail=True` to simulate a non-zero `make` exit, with
    `failure_log` controlling the log written before failing.
    """

    builder: BuilderConfig
    artifact_filename: str = "openwrt-fake-emmc-squashfs-sysupgrade.bin"
    artifact_payload: bytes = b"FAKE_FIRMWARE_PAYLOAD" * 1024  # ~21 KB
    success_log: str = (
        ">>>> fake.profile  Build done in: 01:23.456\n"
    )
    failure_log: str = (
        "config.guess: cannot create a temporary directory in /tmp\n"
        "awk: fatal: print to \"standard output\" failed: No space left on device\n"
        "make: *** [include/owrt2102.mk:163: fake.profile] Error 2\n"
    )
    build_should_fail: bool = False
    build_should_timeout: bool = False
    cancel_during_build: bool = False
    preflight_should_fail: bool = False
    preflight_failure_message: str = "simulated preflight failure"
    on_run_build: Callable[[Path], None] | None = None
    cleanup_should_fail: bool = False

    git_metadata: dict[str, object] = field(
        default_factory=lambda: {
            "git_commit": "abc1234deadbeef5678",
            "git_describe": "abc1234-dirty",
            "git_dirty": True,
        }
    )

    # Recorded interactions for assertions:
    preflight_calls: int = field(default=0)
    run_build_calls: int = field(default=0)
    list_artifacts_calls: list[list[str]] = field(default_factory=list)
    copy_artifact_calls: list[ArtifactCandidate] = field(default_factory=list)
    gather_build_metadata_calls: int = field(default=0)
    run_cleanup_calls: list[list[str]] = field(default_factory=list)

    def build_command(self, *, redact_env: bool = False) -> list[str]:
        cmd = ["docker", "exec", "--workdir", self.builder.workdir]
        for key, value in sorted(self.builder.env.items()):
            cmd.extend(["-e", f"{key}={'<redacted>' if redact_env else value}"])
        cmd.append(self.builder.container)
        cmd.extend(self.builder.command)
        return cmd

    def preflight(self) -> None:
        self.preflight_calls += 1
        if self.preflight_should_fail:
            raise DockerBuildError(self.preflight_failure_message)

    def run_build(self, log_path: Path, *, cancel_token: CancelToken | None = None) -> None:
        self.run_build_calls += 1
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.cancel_during_build and cancel_token is not None:
            cancel_token.request()
        if self.build_should_timeout:
            log_path.write_text("partial output before timeout\n", encoding="utf-8")
            raise DockerBuildError(
                f"build timed out after {self.builder.timeout_sec or 60} seconds"
            )
        if self.build_should_fail:
            log_path.write_text(self.failure_log, encoding="utf-8")
            raise DockerBuildError("build command failed with exit code 2")
        log_path.write_text(self.success_log, encoding="utf-8")
        if self.on_run_build is not None:
            self.on_run_build(log_path)

    def gather_build_metadata(self) -> dict[str, object]:
        self.gather_build_metadata_calls += 1
        return dict(self.git_metadata)

    def run_cleanup(self, command: list[str]) -> str:
        self.run_cleanup_calls.append(list(command))
        if self.cleanup_should_fail:
            raise DockerBuildError(f"simulated cleanup failure: {' '.join(command)}")
        return f"cleaned: {' '.join(command)}\n"

    def list_artifacts(self, patterns: list[str]) -> list[ArtifactCandidate]:
        self.list_artifacts_calls.append(list(patterns))
        return [
            ArtifactCandidate(
                path=f"build/fake/bin/target/{self.artifact_filename}",
                size_bytes=len(self.artifact_payload),
                mtime=1000.0,
            )
        ]

    def copy_artifact(
        self,
        candidate: ArtifactCandidate,
        host_path: Path,
    ) -> ExportedArtifact:
        self.copy_artifact_calls.append(candidate)
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_bytes(self.artifact_payload)
        return ExportedArtifact(
            container_path=f"{self.builder.workdir.rstrip('/')}/{candidate.path}",
            host_path=host_path,
            filename=host_path.name,
            size_bytes=host_path.stat().st_size,
            sha256=sha256_file(host_path),
        )


@dataclass
class FakeFirmwareServer:
    """Stand-in for `TemporaryFirmwareServer` that records calls without binding a port."""

    port: int = 65535
    started: bool = False
    stopped: bool = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def url_for(self, filename: str, *, host: str) -> str:
        return f"http://{host}:{self.port}/{quote(filename)}"
