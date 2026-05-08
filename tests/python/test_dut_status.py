from __future__ import annotations

from owrt_monitor.dut_status import DutStatus, parse_ubus_system_board


def test_parses_typical_openwrt_response() -> None:
    output = (
        "ubus call system board\n"
        "{\n"
        '\t"kernel": "5.15.137",\n'
        '\t"hostname": "OpenWrt",\n'
        '\t"board_name": "mediatek,mt7987",\n'
        '\t"model": "MediaTek MT7987 EVB",\n'
        '\t"release": {\n'
        '\t\t"distribution": "OpenWrt",\n'
        '\t\t"version": "SNAPSHOT",\n'
        '\t\t"revision": "r12345-abcdef",\n'
        '\t\t"target": "mediatek/mt7987"\n'
        "\t}\n"
        "}\n"
        "root@OpenWrt:/# "
    )
    status = parse_ubus_system_board(output)

    assert status.parse_error is None
    assert status.kernel == "5.15.137"
    assert status.hostname == "OpenWrt"
    assert status.board == "mediatek,mt7987"
    assert status.model == "MediaTek MT7987 EVB"
    assert status.release_summary == "OpenWrt SNAPSHOT"


def test_release_summary_falls_back_to_revision_when_no_version() -> None:
    output = '{"hostname":"x","release":{"distribution":"OpenWrt","revision":"r1-abc"}}'
    status = parse_ubus_system_board(output)
    assert status.parse_error is None
    assert status.release_summary == "OpenWrt r1-abc"


def test_returns_parse_error_for_empty_output() -> None:
    status = parse_ubus_system_board("")
    assert status.parse_error == "empty output"
    assert status.kernel is None


def test_returns_parse_error_when_no_json_present() -> None:
    status = parse_ubus_system_board("ubus: command not found\nroot@OpenWrt:/# ")
    assert status.parse_error == "no JSON object found"


def test_returns_parse_error_for_malformed_json() -> None:
    # Balanced braces (so the extractor matches) but invalid JSON inside.
    status = parse_ubus_system_board('{"kernel": broken not-json}')
    assert status.parse_error is not None
    assert "json decode failed" in status.parse_error


def test_returns_parse_error_when_root_is_not_object() -> None:
    status = parse_ubus_system_board("[1, 2, 3]")
    # The greedy `{...}` regex won't match an array, so this falls into "no JSON object".
    assert status.parse_error is not None


def test_to_dict_preserves_release_subset() -> None:
    output = '{"kernel":"5","hostname":"x","release":{"distribution":"OpenWrt"}}'
    status = parse_ubus_system_board(output)
    payload = status.to_dict()
    assert payload["release"] == {"distribution": "OpenWrt"}
    assert payload["kernel"] == "5"
    assert payload["parse_error"] is None


def test_dataclass_default_construction() -> None:
    """Sanity: empty DutStatus is well-formed and renders all-Nones."""
    s = DutStatus()
    assert s.kernel is None
    assert s.release_summary is None
    assert s.to_dict()["release"] is None
