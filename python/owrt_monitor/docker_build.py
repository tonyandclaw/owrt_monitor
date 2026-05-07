from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from owrt_monitor.artifacts import ArtifactCandidate, ExportedArtifact
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

    def run_build(self, log_path: Path) -> None:
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

        if return_code != 0:
            raise DockerBuildError(f"build command failed with exit code {return_code}")

    def list_artifacts(self, patterns: list[str]) -> list[ArtifactCandidate]:
        script = """
import glob
import json
import os

workdir = os.environ["OWRT_WORKDIR"]
patterns = json.loads(os.environ["OWRT_PATTERNS"])
os.chdir(workdir)

seen = {}
for pattern in patterns:
    for path in glob.glob(pattern, recursive=True):
        if os.path.isfile(path):
            stat = os.stat(path)
            seen[path] = {
                "path": path,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }

print(json.dumps(list(seen.values())))
"""
        command = [
            "docker",
            "exec",
            "-e",
            f"OWRT_WORKDIR={self.builder.workdir}",
            "-e",
            f"OWRT_PATTERNS={json.dumps(patterns)}",
            self.builder.container,
            "python3",
            "-c",
            script,
        ]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise DockerBuildError(
                "artifact detection failed; the builder container must provide python3 "
                f"for the MVP detector: {detail}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DockerBuildError(
                f"artifact detector returned invalid JSON: {result.stdout}"
            ) from exc

        return [
            ArtifactCandidate(
                path=item["path"],
                size_bytes=int(item["size_bytes"]),
                mtime=float(item["mtime"]),
            )
            for item in payload
        ]

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
