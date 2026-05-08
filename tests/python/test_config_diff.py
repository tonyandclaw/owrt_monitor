from __future__ import annotations

from pathlib import Path

from owrt_monitor.config_diff import diff_configs, summarize
from owrt_monitor.state import JobState
from owrt_monitor.storage import JobStore


def test_diff_returns_empty_when_identical() -> None:
    snapshot = {"project": {"name": "lab"}, "tests": {"smoke": ["uptime"]}}
    assert diff_configs(snapshot, snapshot) == []


def test_diff_detects_scalar_change() -> None:
    old = {"builder": {"timeout_sec": 0}}
    new = {"builder": {"timeout_sec": 600}}
    changes = diff_configs(old, new)
    assert len(changes) == 1
    assert changes[0].path == "builder.timeout_sec"
    assert changes[0].old == 0
    assert changes[0].new == 600


def test_diff_detects_added_key() -> None:
    old = {"dut": {"name": "dut-01"}}
    new = {"dut": {"name": "dut-01", "expected_artifact_pattern": "emmc"}}
    changes = diff_configs(old, new)
    paths = {c.path for c in changes}
    assert "dut.expected_artifact_pattern" in paths
    added = next(c for c in changes if c.path == "dut.expected_artifact_pattern")
    assert added.old == "<missing>"
    assert added.new == "emmc"


def test_diff_detects_removed_key() -> None:
    old = {"upgrade": {"http_host": "192.168.1.1", "verify_sha256": True}}
    new = {"upgrade": {"verify_sha256": True}}
    changes = diff_configs(old, new)
    assert len(changes) == 1
    assert changes[0].path == "upgrade.http_host"
    assert changes[0].new == "<missing>"


def test_diff_treats_lists_of_different_lengths_as_one_change() -> None:
    old = {"tests": {"smoke": ["a", "b"]}}
    new = {"tests": {"smoke": ["a", "b", "c"]}}
    changes = diff_configs(old, new)
    assert len(changes) == 1
    assert changes[0].path == "tests.smoke"


def test_diff_walks_into_equal_length_lists() -> None:
    old = {"tests": {"smoke": [{"command": "uptime"}, {"command": "x"}]}}
    new = {"tests": {"smoke": [{"command": "uptime"}, {"command": "y"}]}}
    changes = diff_configs(old, new)
    assert len(changes) == 1
    assert changes[0].path == "tests.smoke[1].command"
    assert changes[0].old == "x"
    assert changes[0].new == "y"


def test_summarize_caps_sample() -> None:
    old = {f"k{i}": i for i in range(50)}
    new = {f"k{i}": i + 1 for i in range(50)}
    changes = diff_configs(old, new)
    summary = summarize(changes, sample_limit=10)
    assert summary.total == 50
    assert len(summary.sample) == 10


def test_last_successful_job_excludes_self(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    # No prior jobs → returns None.
    assert store.last_successful_job() is None

    # Old success
    store.create_job(
        job_id="job_old",
        config_path=tmp_path / "c.yaml",
        artifact_dir=tmp_path / "old",
        state=JobState.PENDING.value,
        config_snapshot={"v": 1},
    )
    store.update_job(job_id="job_old", state=JobState.SUCCEEDED.value, result="success")

    # In-progress current
    store.create_job(
        job_id="job_now",
        config_path=tmp_path / "c.yaml",
        artifact_dir=tmp_path / "now",
        state=JobState.PENDING.value,
        config_snapshot={"v": 2},
    )

    last = store.last_successful_job(exclude_id="job_now")
    assert last is not None
    assert last["id"] == "job_old"
    assert last["config_snapshot"] == {"v": 1}


def test_last_successful_job_skips_failed_and_cancelled(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "state.sqlite3")
    for i, result in enumerate(("failed", "cancelled", "dry-run")):
        store.create_job(
            job_id=f"job_{i}",
            config_path=tmp_path / "c.yaml",
            artifact_dir=tmp_path / f"j{i}",
            state="FAILED",
            config_snapshot={"v": i},
        )
        store.update_job(job_id=f"job_{i}", state="FAILED", result=result)
    assert store.last_successful_job() is None
