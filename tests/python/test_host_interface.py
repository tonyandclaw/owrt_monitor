from __future__ import annotations

import subprocess

from owrt_monitor.transfer import infer_host_for_interface


def _completed(
    command: list[str],
    returncode: int,
    stdout: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")


def test_infer_host_for_interface_accepts_bsd_device(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is False
        if command == ["ifconfig", "en7"]:
            return _completed(
                command,
                0,
                "en7: flags=...\n\tinet 192.168.1.66 netmask 0xffffff00\n",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("owrt_monitor.transfer.subprocess.run", fake_run)

    assert infer_host_for_interface("en7") == "192.168.1.66"
    assert calls == [["ifconfig", "en7"]]


def test_infer_host_for_interface_accepts_macos_service_name(monkeypatch) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command == ["ifconfig", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["ip", "-4", "-o", "addr", "show", "dev", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["networksetup", "-listnetworkserviceorder"]:
            return _completed(
                command,
                0,
                """
(1) USB 10/100/1000 LAN
(Hardware Port: USB 10/100/1000 LAN, Device: en8)
""",
            )
        if command == ["ifconfig", "en8"]:
            return _completed(
                command,
                0,
                "en8: flags=...\n\tinet 192.168.1.77 netmask 0xffffff00\n",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("owrt_monitor.transfer.subprocess.run", fake_run)

    assert infer_host_for_interface("USB 10/100/1000 LAN") == "192.168.1.77"


def test_infer_host_for_interface_accepts_macos_hardware_port(monkeypatch) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command == ["ifconfig", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["ip", "-4", "-o", "addr", "show", "dev", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["networksetup", "-listnetworkserviceorder"]:
            return _completed(command, 1, "")
        if command == ["networksetup", "-listallhardwareports"]:
            return _completed(
                command,
                0,
                """
Hardware Port: Wi-Fi
Device: en0

Hardware Port: USB 10/100/1000 LAN
Device: en8
Ethernet Address: 00:11:22:33:44:55
""",
            )
        if command == ["ifconfig", "en8"]:
            return _completed(
                command,
                0,
                "en8: flags=...\n\tinet 192.168.1.88 netmask 0xffffff00\n",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("owrt_monitor.transfer.subprocess.run", fake_run)

    assert infer_host_for_interface("USB 10/100/1000 LAN") == "192.168.1.88"


def test_infer_host_for_interface_ignores_configured_ip_without_active_inet(
    monkeypatch,
) -> None:
    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if command == ["ifconfig", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["ip", "-4", "-o", "addr", "show", "dev", "USB 10/100/1000 LAN"]:
            return _completed(command, 1, "")
        if command == ["networksetup", "-listnetworkserviceorder"]:
            return _completed(
                command,
                0,
                """
(1) USB 10/100/1000 LAN
(Hardware Port: USB 10/100/1000 LAN, Device: en11)
""",
            )
        if command == ["ifconfig", "en11"]:
            return _completed(
                command,
                0,
                "en11: flags=8863<UP,BROADCAST,RUNNING> mtu 1500\n\tstatus: active\n",
            )
        if command == ["ip", "-4", "-o", "addr", "show", "dev", "en11"]:
            return _completed(command, 1, "")
        if command == ["networksetup", "-listallhardwareports"]:
            return _completed(command, 0, "")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("owrt_monitor.transfer.subprocess.run", fake_run)

    assert infer_host_for_interface("USB 10/100/1000 LAN") is None
