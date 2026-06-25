from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from owrt_monitor.artifacts import ArtifactCandidate, ExportedArtifact
from owrt_monitor.cancel import CancelToken, JobCancelled
from owrt_monitor.config import BuilderConfig


class DockerBuildError(RuntimeError):
    """Raised when the Docker builder operation fails."""


class DockerBuildClient:
    def __init__(self, builder: BuilderConfig) -> None:
        self.builder = builder

    def build_command(self, *, redact_env: bool = False) -> list[str]:
        command = ["docker", "exec", "--workdir", self.builder.workdir]
        for key, value in sorted(self.builder.env.items()):
            if redact_env:
                value = "<redacted>"
            command.extend(["-e", f"{key}={value}"])
        command.append(self.builder.container)
        command.extend(self.builder.command)
        return command

    def preflight(self) -> None:
        if shutil.which("docker") is None:
            raise DockerBuildError("docker command was not found")

        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.builder.container],
            check=False,
            capture_output=True,
            text=True,
        )
        if inspect.returncode != 0:
            detail = inspect.stderr.strip() or inspect.stdout.strip()
            raise DockerBuildError(f"cannot inspect container {self.builder.container}: {detail}")
        if inspect.stdout.strip() != "true":
            raise DockerBuildError(f"container {self.builder.container} is not running")

        workdir = subprocess.run(
            ["docker", "exec", self.builder.container, "test", "-d", self.builder.workdir],
            check=False,
            capture_output=True,
            text=True,
        )
        if workdir.returncode != 0:
            raise DockerBuildError(
                f"workdir {self.builder.workdir} does not exist in {self.builder.container}"
            )

        self._preflight_disk_space()
        self._preflight_required_paths()

    def _preflight_required_paths(self) -> None:
        """Confirm each `builder.required_paths` entry exists inside the
        container's workdir. Useful for catching "feeds not checked out"
        before kicking off a 30-minute build.
        """
        paths = self.builder.required_paths
        if not paths:
            return
        missing: list[str] = []
        for path in paths:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--workdir",
                    self.builder.workdir,
                    self.builder.container,
                    "test",
                    "-e",
                    path,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                missing.append(path)
        if missing:
            raise DockerBuildError(
                f"required paths missing inside {self.builder.container}:"
                f"{self.builder.workdir}: {missing}. Run feed/setup steps first."
            )

    def _preflight_disk_space(self) -> None:
        """Fail fast if the workdir filesystem has less free space than configured.

        Uses GNU `df --output=avail -B1` to read available bytes; if `df` errors out
        (different distro, missing util, etc.) the check is skipped silently rather
        than blocking the build on an introspection failure.
        """
        threshold_mb = self.builder.min_free_disk_mb
        if threshold_mb <= 0:
            return
        result = subprocess.run(
            [
                "docker",
                "exec",
                self.builder.container,
                "df",
                "-B1",
                "--output=avail",
                self.builder.workdir,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return  # introspection failed; let the build try and surface the real error
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            return  # no value row
        try:
            avail_bytes = int(lines[-1])
        except ValueError:
            return
        avail_mb = avail_bytes // (1024 * 1024)
        if avail_mb < threshold_mb:
            raise DockerBuildError(
                f"insufficient disk in {self.builder.container}:{self.builder.workdir}: "
                f"{avail_mb} MB free, need at least {threshold_mb} MB. "
                "Free space (e.g. `docker system prune -a` or clear stale build_dir trees) "
                "and retry."
            )

    def run_build(self, log_path: Path, *, cancel_token: CancelToken | None = None) -> None:
        command = self.build_command()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timeout = self.builder.timeout_sec or None

        with log_path.open("w", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            watcher = None
            if cancel_token is not None:
                watcher = cancel_token.watch(process.terminate)
                watcher.__enter__()
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    sys.stdout.write(line)
                return_code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise DockerBuildError(f"build timed out after {timeout} seconds") from exc
            finally:
                if watcher is not None:
                    watcher.__exit__(None, None, None)

        if cancel_token is not None and cancel_token.is_cancelled:
            raise JobCancelled("build terminated by cancel request")
        if return_code != 0:
            raise DockerBuildError(f"build command failed with exit code {return_code}")

    def run_cleanup(self, command: list[str]) -> str:
        """Run a maintenance command (argument array, no shell) in the builder
        workdir — e.g. `["make", "-C", "build/owrt2102", "package/x/clean"]`.

        Used by the profile-switch guard to clean profile-conditional packages
        before a build. Carries the same `builder.env` as the build so commands
        that need it (e.g. FORCE_UNSAFE_CONFIGURE) behave identically. Returns
        captured stdout/stderr; raises DockerBuildError on a non-zero exit.
        """
        if not command:
            raise DockerBuildError("cleanup command must contain at least one argument")
        full_command = ["docker", "exec", "--workdir", self.builder.workdir]
        for key, value in sorted(self.builder.env.items()):
            full_command.extend(["-e", f"{key}={value}"])
        full_command.append(self.builder.container)
        full_command.extend(command)
        result = subprocess.run(full_command, check=False, capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip() or "no output")
            raise DockerBuildError(
                f"cleanup command {' '.join(command)!r} failed with exit "
                f"code {result.returncode}: {detail}"
            )
        return output

    def list_artifacts(self, patterns: list[str]) -> list[ArtifactCandidate]:
        # Bash-only detector: uses `shopt -s globstar nullglob` for `**` support and
        # GNU `stat -c` for size + mtime. Avoids depending on python3 inside the
        # builder image. Output is one TAB-separated record per file:
        #     <size_bytes>\t<mtime_epoch>\t<relative_path>
        script = (
            "set -eu\n"
            'shopt -s globstar nullglob\n'
            'cd "$OWRT_WORKDIR"\n'
            'for pattern in "$@"; do\n'
            '  for f in $pattern; do\n'
            '    [ -f "$f" ] || continue\n'
            '    sz=$(stat -c %s -- "$f")\n'
            '    mt=$(stat -c %Y -- "$f")\n'
            '    printf "%s\\t%s\\t%s\\n" "$sz" "$mt" "$f"\n'
            '  done\n'
            'done\n'
        )
        command = [
            "docker",
            "exec",
            "-e",
            f"OWRT_WORKDIR={self.builder.workdir}",
            self.builder.container,
            "bash",
            "-c",
            script,
            "_artifact_detector",
            *patterns,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DockerBuildError(f"artifact detection failed: {detail}")

        candidates: dict[str, ArtifactCandidate] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) != 3:
                raise DockerBuildError(f"artifact detector returned malformed line: {line!r}")
            size_str, mtime_str, path = parts
            try:
                size_bytes = int(size_str)
                mtime = float(mtime_str)
            except ValueError as exc:
                raise DockerBuildError(
                    f"artifact detector returned non-numeric size/mtime in line: {line!r}"
                ) from exc
            candidates[path] = ArtifactCandidate(
                path=path,
                size_bytes=size_bytes,
                mtime=mtime,
            )
        return list(candidates.values())

    def gather_build_metadata(self) -> dict[str, object]:
        """Best-effort capture of build provenance from the workdir's git state.

        Failures (no git, no commit, etc.) become `None` rather than raising —
        the artifact still has a SHA256 even if provenance is unavailable.
        Uses subprocess argument-list form throughout (no shell interpretation).
        """

        def _capture(*args: str) -> str | None:
            command = [
                "docker",
                "exec",
                "--workdir",
                self.builder.workdir,
                self.builder.container,
                *args,
            ]
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                return None
            return result.stdout.strip() or None

        commit = _capture("git", "rev-parse", "HEAD")
        describe = _capture("git", "describe", "--tags", "--always", "--dirty")
        status = _capture("git", "status", "--porcelain")
        return {
            "git_commit": commit,
            "git_describe": describe,
            "git_dirty": bool(status) if status is not None else None,
        }

    def copy_artifact(self, candidate: ArtifactCandidate, host_path: Path) -> ExportedArtifact:
        host_path.parent.mkdir(parents=True, exist_ok=True)
        container_path = f"{self.builder.workdir.rstrip('/')}/{candidate.path}"
        result = subprocess.run(
            ["docker", "cp", f"{self.builder.container}:{container_path}", str(host_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DockerBuildError(f"docker cp failed for {container_path}: {detail}")

        return ExportedArtifact(
            container_path=container_path,
            host_path=host_path,
            filename=host_path.name,
            size_bytes=host_path.stat().st_size,
            sha256=sha256_file(host_path),
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
