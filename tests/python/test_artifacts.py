from owrt_monitor.artifacts import ArtifactCandidate, ArtifactSelectionError, select_artifact


def test_select_newest_artifact() -> None:
    selected = select_artifact(
        [
            ArtifactCandidate("old.bin", size_bytes=10, mtime=1),
            ArtifactCandidate("new.bin", size_bytes=10, mtime=2),
        ],
        selection="newest",
    )

    assert selected.path == "new.bin"


def test_select_largest_artifact() -> None:
    selected = select_artifact(
        [
            ArtifactCandidate("small.bin", size_bytes=10, mtime=3),
            ArtifactCandidate("large.bin", size_bytes=20, mtime=1),
        ],
        selection="largest",
    )

    assert selected.path == "large.bin"


def test_fail_if_multiple_artifacts() -> None:
    try:
        select_artifact(
            [
                ArtifactCandidate("a.bin", size_bytes=10, mtime=1),
                ArtifactCandidate("b.bin", size_bytes=10, mtime=2),
            ],
            selection="fail-if-multiple",
        )
    except ArtifactSelectionError as exc:
        assert "expected one artifact" in str(exc)
    else:
        raise AssertionError("expected ArtifactSelectionError")


def test_min_size_threshold() -> None:
    try:
        select_artifact(
            [ArtifactCandidate("tiny.bin", size_bytes=100, mtime=1)],
            selection="newest",
            min_size_mb=1,
        )
    except ArtifactSelectionError as exc:
        assert "size threshold" in str(exc)
    else:
        raise AssertionError("expected ArtifactSelectionError")
