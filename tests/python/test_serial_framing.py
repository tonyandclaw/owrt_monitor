from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from owrt_monitor.config import ConfigError, OwrtConfig, load_config
from owrt_monitor.dut_serial import _PARITY_MAP, SerialSession


def _load(tmp_path: Path, dut: dict) -> OwrtConfig:
    raw = {
        "project": {"artifact_dir": str(tmp_path / "artifacts")},
        "builder": {"container": "x", "workdir": "/w", "command": ["m"]},
        "artifact": {"patterns": ["*.bin"]},
        "dut": dut,
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return load_config(p)


def test_default_framing_is_8_n_1(tmp_path: Path) -> None:
    cfg = _load(tmp_path, {"serial": "/dev/fake"})
    assert cfg.dut.bytesize == 8
    assert cfg.dut.parity == "none"
    assert cfg.dut.stopbits == 1


def test_custom_framing_validates(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {"serial": "/dev/fake", "bytesize": 7, "parity": "even", "stopbits": 2},
    )
    assert cfg.dut.bytesize == 7
    assert cfg.dut.parity == "even"
    assert cfg.dut.stopbits == 2


def test_invalid_framing_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        _load(tmp_path, {"serial": "/dev/fake", "bytesize": 9})  # not in {5,6,7,8}
    with pytest.raises(ConfigError):
        _load(tmp_path, {"serial": "/dev/fake", "parity": "weird"})
    with pytest.raises(ConfigError):
        _load(tmp_path, {"serial": "/dev/fake", "stopbits": 3})


def test_serial_session_stores_framing(tmp_path: Path) -> None:
    """The session passes framing through to pyserial without owning the import."""
    session = SerialSession(
        port="/dev/fake",
        baud=115200,
        prompt=r"# ",
        transcript_path=tmp_path / "serial.log",
        bytesize=7,
        parity="odd",
        stopbits=2,
    )
    assert session.bytesize == 7
    assert session.parity == "odd"
    assert session.stopbits == 2


def test_parity_map_covers_documented_values() -> None:
    assert _PARITY_MAP == {
        "none": "N",
        "even": "E",
        "odd": "O",
        "mark": "M",
        "space": "S",
    }
