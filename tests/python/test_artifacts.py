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


def test_regex_filter_keeps_matching_only() -> None:
    selected = select_artifact(
        [
            ArtifactCandidate("build/foo/openwrt-emmc.bin", size_bytes=10, mtime=1),
            ArtifactCandidate("build/foo/openwrt-sd.bin", size_bytes=20, mtime=2),
        ],
        selection="newest",
        regex_patterns=[r"emmc"],
    )
    # Even though "sd" is newer, the regex filter rejects it.
    assert selected.path == "build/foo/openwrt-emmc.bin"


def test_regex_filter_requires_all_patterns_match() -> None:
    selected = select_artifact(
        [
            ArtifactCandidate("a/openwrt-emmc.bin", size_bytes=10, mtime=1),
            ArtifactCandidate("b/openwrt-emmc.bin", size_bytes=10, mtime=2),
        ],
        selection="newest",
        regex_patterns=[r"emmc", r"^a/"],
    )
    assert selected.path == "a/openwrt-emmc.bin"


def test_regex_filter_with_no_match_raises() -> None:
    import pytest
    with pytest.raises(ArtifactSelectionError, match=r"regex_patterns"):
        select_artifact(
            [ArtifactCandidate("openwrt-sd.bin", size_bytes=10, mtime=1)],
            selection="newest",
            regex_patterns=[r"emmc"],
        )


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
