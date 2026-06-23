from __future__ import annotations

from pathlib import Path

from owrt_monitor.build_log import classify_build_log

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "build_logs"


def test_classify_real_success_log() -> None:
    summary = classify_build_log(FIXTURES / "success_owrt2102_ap.log")

    assert summary.success is True
    assert summary.classification == "success"
    assert summary.duration_sec is not None
    # Real run was "05:07.537" — assert order of magnitude rather than exact seconds.
    assert 60 <= summary.duration_sec <= 3600
    assert summary.failed_target is None
    assert summary.failed_step is None
    # Lab's enw-device-firmware emits a known set of dependency warnings; assert one we know.
    assert any("kmod-nf-flow-netlink" in w for w in summary.warnings)


def test_classify_real_disk_full_log() -> None:
    summary = classify_build_log(FIXTURES / "disk_full.log")

    assert summary.success is False
    assert summary.classification == "disk_full"
    assert summary.failed_target == "owrt2102.asus_eap5000_mt7987"
    # Evidence should include at least one of the canonical disk-full lines.
    assert any("No space left on device" in line for line in summary.evidence)


def test_classify_failed_package_synthetic(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    log.write_text(
        "make -C package/foo compile\n"
        "gcc: fatal error: bar.h: No such file or directory\n"
        "make[3]: *** [package/foo/compile] Error 2\n"
        "make[2]: *** [/build/include/toplevel.mk:228: world] Error 2\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_eap5000_mt7987] Error 2\n",
        encoding="utf-8",
    )

    summary = classify_build_log(log)

    assert summary.classification == "failed_package"
    assert summary.failed_step == "package/foo/compile"
    assert summary.failed_package == "foo"
    assert summary.failed_target == "owrt2102.asus_eap5000_mt7987"


def test_extract_failed_package_from_nested_path(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    log.write_text(
        "make[4]: *** [package/feeds/mtk/flowtable/install] Error 2\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_eap5000_mt7987] Error 2\n",
        encoding="utf-8",
    )
    summary = classify_build_log(log)
    assert summary.classification == "failed_package"
    assert summary.failed_package == "feeds/mtk/flowtable"


def test_extract_failed_package_for_target_subdir(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    log.write_text(
        "make[3]: *** [target/linux/install] Error 2\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_eap5000_mt7987] Error 2\n",
        encoding="utf-8",
    )
    summary = classify_build_log(log)
    assert summary.failed_package == "target/linux"


def test_failed_package_is_none_for_synthetic_world_target(tmp_path: Path) -> None:
    """The propagated `world` target isn't a real package and shouldn't be reported."""
    log = tmp_path / "build.log"
    # Only the toplevel/world failure exists; no underlying package fail.
    log.write_text(
        "make[2]: *** [/build/include/toplevel.mk:228: world] Error 2\n"
        "make: *** [include/owrt2102.mk:163: owrt2102.asus_eap5000_mt7987] Error 2\n",
        encoding="utf-8",
    )
    summary = classify_build_log(log)
    # The toplevel.mk match still classifies as failed_package via _PACKAGE_FAIL,
    # but `_extract_failed_package` correctly returns None for synthetic world.
    assert summary.failed_package is None


def test_classify_unknown_when_no_pattern_matches(tmp_path: Path) -> None:
    log = tmp_path / "build.log"
    log.write_text("some unrelated chatter\nmore chatter\n", encoding="utf-8")

    summary = classify_build_log(log)

    assert summary.success is False
    assert summary.classification == "unknown"
    # Tail evidence should be the last non-blank lines.
    assert "more chatter" in summary.evidence


def test_classify_missing_log(tmp_path: Path) -> None:
    summary = classify_build_log(tmp_path / "does_not_exist.log")

    assert summary.success is False
    assert summary.classification == "missing_log"
